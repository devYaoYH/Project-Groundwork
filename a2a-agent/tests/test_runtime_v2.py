import asyncio
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from a2a_engine.remote.contract import JoinRequest, PROTOCOL_VERSION, TurnInvocation, TurnInvocationV2
from a2a_engine.remote.network import mcp_call
from calendar_game.agents import GameConfig
from calendar_game.game import CalendarGame
from calendar_game.remote import RemoteSeatClient


def client_for(harness):
    game = CalendarGame({"num_agents": 2, "agents": [{"type": "scripted"}] * 2})
    return RemoteSeatClient(harness.context, 0, game.router)


def register_invocation(client):
    episode = client.runtime.episode
    root = f"{client.runtime.io.base_url}/episodes/{episode.episode_id}"
    return TurnInvocationV2(environment_id=episode.environment_id, episode_id=episode.episode_id,
        attempt_id=episode.attempt_id, seat=client.seat.seat, turn_id=uuid.uuid4().hex,
        kind="register", operation="register", deadline=datetime.now(timezone.utc) + timedelta(seconds=10),
        mcp={name: f"{root}/{name}/mcp" for name in ("env", "comm", "lifecycle")})


def opened(harness, client, kind="register"):
    value = register_invocation(client) if kind == "register" else client._invocation(kind)
    record = harness.environment.registry.lifecycle.open(value, deadline=time.monotonic() + 10)
    return value, record


def start(harness, value):
    return asyncio.run(mcp_call(harness.callback.base_url + "/mcp", harness.episode.seats[0].secret,
                                "start_turn", value.model_dump(mode="json")))


def test_atomic_start_dedup_conflict_busy_and_capacity(remote_harness_v2):
    harness = remote_harness_v2()
    client = client_for(harness)
    harness.policies[0].calls = lambda value: asyncio.sleep(10, result=[])
    value, pending = opened(harness, client)

    async def duplicates():
        return await asyncio.gather(*[mcp_call(harness.callback.base_url + "/mcp", client.seat.secret,
                                               "start_turn", value.model_dump(mode="json")) for _ in range(4)])

    results = asyncio.run(duplicates())
    assert all(r == results[0] and r["status"] == "accepted" for r in results)
    assert len(harness.runtime.records) == 1 and not pending.event.is_set()
    changed = value.model_copy(update={"prompt": "changed"})
    assert start(harness, changed)["reason_code"] == "conflict"
    another, _ = opened(harness, client)
    assert start(harness, another)["reason_code"] == "busy"
    harness.environment.registry.lifecycle.close(pending, "cancelled")
    harness.callback.run(harness.runtime.close())
    assert not harness.runtime.sessions and not harness.runtime.records


def test_session_and_record_capacity_never_evict_active(remote_harness_v2):
    harness = remote_harness_v2(seats=(0, 1))
    harness.runtime.max_records = 1
    harness.policies[0].calls = lambda value: asyncio.sleep(10, result=[])
    client = client_for(harness)
    value, _ = opened(harness, client)
    assert start(harness, value)["status"] == "accepted"
    other = RemoteSeatClient(harness.context, 1, client.router)
    next_value, _ = opened(harness, other)
    result = asyncio.run(mcp_call(harness.callback.base_url + "/mcp", other.seat.secret,
                                 "start_turn", next_value.model_dump(mode="json")))
    assert result["reason_code"] == "capacity" and len(harness.runtime.records) == 1
    harness.runtime.max_sessions = 2
    with pytest.raises(ValueError, match="capacity"):
        harness.callback.run(harness.runtime.join(harness.io.base_url + "/episodes/other/join",
                                                  harness.callback.base_url, "x" * 32))


@pytest.mark.parametrize("field,value", [("seat", 1), ("attempt_id", "wrong"), ("environment_id", "wrong"),
                                         ("mcp", {"env": "http://127.0.0.1:1/mcp"})])
def test_invocation_is_authenticated_from_admitted_identity(remote_harness_v2, field, value):
    harness = remote_harness_v2()
    invocation, _ = opened(harness, client_for(harness))
    with pytest.raises(ValueError, match="rejected"):
        start(harness, invocation.model_copy(update={field: value}))
    assert not harness.runtime.records


def test_register_then_first_turn_before_completion_receipt_is_received(remote_harness_v2, monkeypatch):
    harness = remote_harness_v2()
    registry = harness.environment.registry.lifecycle
    finish = registry.finish
    held = threading.Event()
    release = threading.Event()

    def delay_receipt(credential, finished):
        receipt = finish(credential, finished)
        pending = registry.records[registry.key(finished)]
        if pending.invocation.kind == "register":
            held.set()
            assert release.wait(3)
        return receipt

    monkeypatch.setattr(registry, "finish", delay_receipt)
    client = client_for(harness)
    try:
        harness.register_client(client, GameConfig(2, 3, 0, [0, 1]))
        assert held.is_set()
        register_record = next(iter(harness.runtime.records.values()))
        assert register_record.finished.status == "succeeded" and register_record.receipt is None
        client.start_round({"id": 0, "participants": [0, 1], "cost": 1, "duration": 1}, "Slot 0: [FREE]", 0)
        client.prepare({"phase": "DECISION", "round": 0, "turn": 0, "prompt_sent": "decide",
                        "calendar_snapshot_render": "Slot 0: [FREE]"}, 0)
        result = client.decide({}, "")
        assert result.tool_calls == [{"type": "schedule", "meeting_id": 0, "slot": 0}]
        assert client.seat.recorder.closed_by == "completed"
    finally:
        release.set()


def test_runtime_crash_discards_sessions_and_environment_expires(remote_harness_v2):
    harness = remote_harness_v2()
    client = client_for(harness)
    harness.policies[0].calls = lambda value: asyncio.sleep(10, result=[])
    value = register_invocation(client)
    registry = harness.environment.registry.lifecycle
    record = registry.open(value, deadline=time.monotonic() + 0.15)
    assert start(harness, value)["status"] == "accepted"
    # No durable recovery exists. Simulate process state loss, not a retry of a new turn.
    harness.callback.run(harness.runtime.close())
    time.sleep(0.2)
    registry._expire()
    assert record.event.is_set() and record.closed_by in {"failed", "deadline"}
    assert not harness.runtime.sessions


def test_both_protocols_are_explicit_and_no_global_version_replacement(remote_harness_v2):
    harness = remote_harness_v2()
    assert PROTOCOL_VERSION == "a2a-turns/1"
    assert harness.episode.seats[0].protocol_version == "a2a-turns/2"
    value = register_invocation(client_for(harness))
    with pytest.raises(ValueError):
        TurnInvocation.model_validate(value.model_dump())
    request = JoinRequest(join_ticket="x" * 32, callback_url=harness.callback.base_url,
                          protocol_versions=["a2a-turns/3"])
    from a2a_engine.remote.admission import join
    with pytest.raises(ValueError, match="unsupported"):
        harness.io.run(join(harness.episode, request, harness.io.base_url))


def test_provider_thread_prevents_subsequent_session_use(remote_harness_v2):
    harness = remote_harness_v2()
    client = client_for(harness)
    harness.register_client(client, GameConfig(2, 3, 0, [0, 1]))
    lock = threading.Lock()
    lock.acquire()
    harness.policies[0].model_lock = lock
    value, _ = opened(harness, client, "round_start")
    assert start(harness, value)["reason_code"] == "busy"
    lock.release()


def test_host_origin_and_body_bounds_on_agent_surface(remote_harness_v2):
    harness = remote_harness_v2()
    with httpx.Client(trust_env=False) as client:
        assert client.get(harness.callback.base_url + "/health", headers={"host": "evil"}).status_code == 403
        assert client.get(harness.callback.base_url + "/health", headers={"origin": "http://evil"}).status_code == 403
        assert client.post(harness.callback.base_url + "/mcp", content=b"x" * 262145).status_code == 413


def test_confirmed_busy_rejection_closes_pending_record_promptly(remote_harness_v2):
    from a2a_engine.remote.dispatch import DeliveryFailure
    harness = remote_harness_v2()
    client = client_for(harness)
    harness.policies[0].calls = lambda value: asyncio.sleep(10, result=[])
    value, _ = opened(harness, client)
    assert start(harness, value)["status"] == "accepted"
    next_value = register_invocation(client)
    start_time = time.monotonic()
    with pytest.raises(DeliveryFailure) as failure:
        client.dispatcher.dispatch(client.seat, next_value)
    assert failure.value.closed_by == "rejected" and time.monotonic() - start_time < 1
    record = harness.environment.registry.lifecycle.records[harness.environment.registry.lifecycle.key(next_value)]
    assert record.event.is_set() and record.acceptance.reason_code == "busy"


def test_terminal_records_and_ended_sessions_expire_but_active_tasks_do_not(remote_harness_v2):
    harness = remote_harness_v2()
    client = client_for(harness)
    stage = harness.register_client(client, GameConfig(2, 3, 0, [0, 1]))
    stage.__exit__(None, None, None)
    # Wait until independent receipt delivery has released its bounded task.
    deadline = time.monotonic() + 2
    while not all(record.task.done() for record in harness.runtime.records.values()):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    harness.runtime.clock = lambda: time.monotonic() + 4000
    harness.callback.run(harness.runtime.health(None))
    assert not harness.runtime.records and not harness.runtime.sessions


def test_duplicate_start_after_execution_returns_acceptance_without_reexecution(remote_harness_v2):
    harness = remote_harness_v2()
    client = client_for(harness)
    # A successful registration updates policy/history only once.
    harness.register_client(client, GameConfig(2, 3, 0, [0, 1]))
    value = harness.invocations[0]
    record = next(iter(harness.runtime.records.values()))
    assert record.finished.status == "succeeded"
    assert start(harness, value)["status"] == "accepted"
    assert len(harness.invocations) == 1


def test_long_lived_runtime_isolates_episode_attempt_sessions(remote_harness_v2):
    from calendar_game.remote import CalendarRuntimeContext
    from a2a_agent.scripted import ScriptedPolicy
    harness = remote_harness_v2()
    client = client_for(harness)
    stage = harness.register_client(client, GameConfig(2, 3, 0, [0, 1]))
    episode = harness.environment.registry.provision("second-episode", [0])
    policy = ScriptedPolicy()
    try:
        admission = harness.callback.run(harness.runtime.join(
            f"{harness.io.base_url}/episodes/second-episode/join", harness.callback.base_url,
            episode.seats[0].ticket, policy=policy))
        assert admission.attempt_id != harness.episode.attempt_id
        other = RemoteSeatClient(CalendarRuntimeContext(harness.environment, harness.io, episode), 0, client.router)
        other_stage = harness.register_client(other, GameConfig(2, 7, 0, [0, 1]))
        assert harness.policies[0].config["num_slots"] == 3 and policy.config["num_slots"] == 7
        stage.__exit__(None, None, None)
        harness.environment.registry.unregister(harness.episode)
        other.start_round({"id": 3, "participants": [0, 1], "duration": 1, "cost": 1}, "Slot 0: [FREE]", 0)
        sessions = list(harness.runtime.sessions.values())
        assert len(sessions) == 2 and sum(s.ended for s in sessions) == 1
        assert episode.active and episode.seats[0].registered
        other_stage.__exit__(None, None, None)
    finally:
        harness.environment.registry.unregister(episode)
