import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from a2a_engine.remote.dispatch import signed_headers
from a2a_engine.remote.contract import TurnInvocation


def value(harness, identifier="register", kind="register", seconds=3, **kwargs):
    admission = harness.runtimes[0].admission
    return TurnInvocation(episode_id=admission.episode_id, seat=0, turn_id=identifier, kind=kind,
                          deadline=datetime.now(timezone.utc) + timedelta(seconds=seconds),
                          mcp=admission.mcp, observation={"game_config": {"communication_protocol": "dm"}}, **kwargs)


async def send(client, harness, invocation):
    body = invocation.model_dump_json().encode()
    return await client.post(harness.callbacks[0].base_url + "/turns", content=body,
                             headers=signed_headers(body, invocation.turn_id, invocation.deadline.isoformat(),
                                                    harness.runtimes[0].admission.seat_secret))


def test_inflight_duplicates_share_execution_and_conflicts_fail_immediately(remote_harness):
    harness = remote_harness()
    runtime = harness.runtimes[0]
    executed = []
    original = runtime.policy.calls

    async def slow(invocation):
        executed.append(invocation.turn_id)
        await asyncio.sleep(0.2)
        return original(invocation)

    runtime.policy.calls = slow
    invocation = value(harness)

    async def check():
        async with httpx.AsyncClient(trust_env=False) as client:
            tasks = [asyncio.create_task(send(client, harness, invocation)) for _ in range(8)]
            await asyncio.sleep(0.08)
            conflict = await send(client, harness, invocation.model_copy(update={"prompt": "changed"}))
            assert conflict.status_code == 409
            results = await asyncio.gather(*tasks)
            assert all(response.status_code == 200 for response in results)
            assert all(response.json() == results[0].json() for response in results)
            assert (await send(client, harness, invocation)).json() == results[0].json()

    asyncio.run(check())
    assert executed == ["register"] and not runtime.inflight


def test_model_memory_serializes_distinct_invocations_and_includes_lock_wait_in_deadline(remote_harness):
    harness = remote_harness()
    runtime = harness.runtimes[0]
    runtime.registered = True
    active = [0]
    max_active = [0]
    executed = []

    async def slow(invocation):
        active[0] += 1
        max_active[0] = max(active[0], max_active[0])
        executed.append(invocation.turn_id)
        try:
            await asyncio.sleep(0.25)
        finally:
            active[0] -= 1
        return []

    runtime.policy.calls = slow

    async def check():
        async with httpx.AsyncClient(trust_env=False) as client:
            first = asyncio.create_task(send(client, harness, value(harness, "long", "round_start")))
            await asyncio.sleep(0.05)
            second = await send(client, harness, value(harness, "short", "round_start", seconds=0.08))
            assert second.status_code == 408
            assert (await first).status_code == 200

    asyncio.run(check())
    assert executed == ["long"] and max_active == [1] and active == [0]
    assert not runtime.inflight


def test_hung_execution_cancels_and_releases_memory_lock(remote_harness):
    harness = remote_harness()
    runtime = harness.runtimes[0]
    runtime.registered = True
    cancelled = []

    async def hung(invocation):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.append(invocation.turn_id)

    runtime.policy.calls = hung

    async def check():
        async with httpx.AsyncClient(trust_env=False) as client:
            result = await send(client, harness, value(harness, "hung", "round_start", seconds=0.08))
            assert result.status_code == 408
            runtime.policy.calls = lambda invocation: []
            assert (await send(client, harness, value(harness, "next", "round_start"))).status_code == 200

    asyncio.run(check())
    assert cancelled == ["hung"] and not runtime.lock.locked() and not runtime.inflight


def test_completed_cache_is_bounded_expires_and_never_evicts_live_dedup(remote_harness):
    harness = remote_harness()
    runtime = harness.runtimes[0]
    runtime.max_cached = 2
    now = [0.0]
    runtime.clock = lambda: now[0]
    first = value(harness)

    async def check():
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await send(client, harness, first)).status_code == 200
            assert (await send(client, harness, value(harness, "second", "round_start"))).status_code == 200
            assert (await send(client, harness, value(harness, "third", "round_start"))).status_code == 503
            assert (await send(client, harness, first)).status_code == 200
            assert len(runtime.completed) == 2
            now[0] = 4
            assert (await send(client, harness, value(harness, "fourth", "round_start"))).status_code == 200

    asyncio.run(check())
    assert len(runtime.completed) == 1


def test_inflight_capacity_and_disconnected_waiter_do_not_duplicate_execution(remote_harness):
    harness = remote_harness()
    runtime = harness.runtimes[0]
    runtime.max_inflight = 1
    executed = []
    original = runtime.policy.calls

    async def slow(invocation):
        executed.append(invocation.turn_id)
        await asyncio.sleep(0.15)
        return original(invocation)

    runtime.policy.calls = slow
    invocation = value(harness)

    async def check():
        async with httpx.AsyncClient(trust_env=False) as client:
            task = asyncio.create_task(send(client, harness, invocation))
            await asyncio.sleep(0.05)
            assert (await send(client, harness, value(harness, "other"))).status_code == 503
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert (await send(client, harness, invocation)).status_code == 200

    asyncio.run(check())
    assert executed == ["register"]


def test_failed_execution_is_cached_and_sanitized(remote_harness):
    harness = remote_harness()
    executed = []

    def broken(invocation):
        executed.append(invocation.turn_id)
        raise ValueError(harness.runtimes[0].admission.seat_secret)

    harness.runtimes[0].policy.calls = broken
    invocation = value(harness)

    async def check():
        async with httpx.AsyncClient(trust_env=False) as client:
            first = await send(client, harness, invocation)
            second = await send(client, harness, invocation)
            assert first.status_code == second.status_code == 500
            assert first.json() == second.json() == {"code": "execution_failed"}

    asyncio.run(check())
    assert executed == ["register"]


def test_episode_end_replay_returns_cached_completion(remote_harness):
    harness = remote_harness()
    runtime = harness.runtimes[0]
    invocation = value(harness, "end", "episode_end")

    async def check():
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await send(client, harness, value(harness))).status_code == 200
            first = await send(client, harness, invocation)
            replay = await send(client, harness, invocation)
            assert first.status_code == replay.status_code == 200
            assert first.json() == replay.json()
            assert (await send(client, harness, value(harness, "after", "round_start"))).status_code == 409

    asyncio.run(check())
    assert runtime.ended.is_set()
    assert [item.turn_id for item in harness.invocations] == ["register", "end"]


def test_reflection_skips_action_policy_and_bounds_cached_completion(remote_harness):
    harness = remote_harness()
    runtime = harness.runtimes[0]
    runtime.registered = True
    executed = []

    def action(invocation):
        pytest.fail("reflection must not enter the conversation/action policy")

    def reflect(invocation):
        executed.append(invocation.turn_id)
        return {"padding": "x" * 300000}

    runtime.policy.calls = action
    runtime.policy.reflect = reflect
    invocation = value(harness, "reflect", "reflect")

    async def check():
        async with httpx.AsyncClient(trust_env=False) as client:
            first = await send(client, harness, invocation)
            replay = await send(client, harness, invocation)
            assert first.status_code == replay.status_code == 500
            assert first.json() == replay.json() == {"code": "execution_failed"}

    asyncio.run(check())
    assert executed == ["reflect"]


def test_mcp_lost_response_retries_stable_call_id(open_remote_turn, monkeypatch):
    from contextlib import asynccontextmanager
    from a2a_agent import mcp_client

    harness = open_remote_turn
    original = mcp_client.connect
    attempts = []

    @asynccontextmanager
    async def flaky(*args, **kwargs):
        async with original(*args, **kwargs) as session:
            class Proxy:
                async def call_tool(self, name, arguments, meta):
                    result = await session.call_tool(name, arguments, meta=meta)
                    attempts.append(meta["a2a/call_id"])
                    if len(attempts) == 1:
                        raise httpx.ReadError("lost response")
                    return result
            yield Proxy()

    monkeypatch.setattr(mcp_client, "connect", flaky)
    results = asyncio.run(mcp_client.execute(harness.client.pending, [("env", "schedule", {"meeting_id": 0, "slot": 0})]))
    assert results[0]["status"] == "accepted"
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert len(harness.client.seat.recorder.records) == 1
