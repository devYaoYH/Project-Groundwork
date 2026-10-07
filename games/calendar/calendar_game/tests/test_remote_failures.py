import asyncio
import threading
import time

import httpx
import pytest

from a2a_agent.mcp_client import connect, execute
from calendar_game.game import CalendarGame


def config(**overrides):
    return {"num_agents": 2, "num_slots": 3, "decision_retries": 0, "enable_reflection": False,
            "enable_fallback": False, "max_turns_per_round": 1,
            "agents": [{"type": "scripted", "runtime": "external"}, {"type": "scripted"}],
            "turn_timeout_s": {"DECISION": 0.3, "register": 1, "round_start": 1, "episode_end": 0.3}, **overrides}


def task():
    return {"calendars": [[None] * 3, [None] * 3], "meetings": [
        {"id": 0, "participants": [0, 1], "speaker_order": [0, 1], "duration": 1, "cost": 1}]}


@pytest.mark.parametrize("staged", [False, True])
def test_deadline_keeps_already_staged_cells_and_never_falls_back(remote_harness, staged):
    harness = remote_harness()
    runtime = harness.runtimes[0]
    original = runtime.policy.calls
    cancelled = []

    async def slow(invocation):
        if invocation.phase != "DECISION":
            return original(invocation)
        if staged:
            await execute(invocation, [("env", "schedule", {"meeting_id": 0, "slot": 0})])
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)
        return []

    runtime.policy.calls = slow
    start = time.monotonic()
    trace = CalendarGame(config(), runtime_context=harness.context).run_with_scenario(task())
    assert time.monotonic() - start < 2
    end = next(event for event in trace.events if event.type == "decide_end" and event.data["agent_id"] == 0)
    assert end.data["closed_by"] == "deadline" and end.data["telemetry_source"] == "environment"
    assert trace.metrics["meetings_scheduled"] == int(staged)
    assert end.data["tool_calls"] == ([{"type": "schedule", "meeting_id": 0, "slot": 0}] if staged else [])
    assert cancelled == [True] and not runtime.inflight
    assert not harness.episode.active and harness.episode.seats[0].secret is None


def test_unreachable_turn_returns_staged_actions_and_sanitized_closure(remote_harness, monkeypatch):
    from a2a_engine.remote import dispatch

    harness = remote_harness()
    original = dispatch.post_signed
    calls = []

    async def lost(url, invocation, key, **kwargs):
        if invocation.phase == "DECISION":
            if not calls:
                await original(url, invocation, key, **kwargs)
            calls.append(invocation.turn_id)
            raise httpx.ReadError(key)
        return await original(url, invocation, key, **kwargs)

    monkeypatch.setattr(dispatch, "post_signed", lost)
    secret = harness.episode.seats[0].secret
    trace = CalendarGame(config(turn_timeout_s=2), runtime_context=harness.context).run_with_scenario(task())
    end = next(event for event in trace.events if event.type == "decide_end" and event.data["agent_id"] == 0)
    assert end.data["closed_by"] == "unreachable" and end.data["attempts"] == 3
    assert trace.metrics["meetings_scheduled"] == 1
    assert len(calls) == 3 and len(set(calls)) == 1
    assert len([invocation for invocation in harness.invocations if invocation.phase == "DECISION"]) == 1
    assert secret not in trace.model_dump_json()


def test_lost_push_response_deduplicates_messages_and_budget_charging(remote_harness, monkeypatch):
    from a2a_engine.remote import dispatch

    harness = remote_harness()
    original = dispatch.post_signed
    lost_ids = set()

    async def once(url, invocation, key, **kwargs):
        body = await original(url, invocation, key, **kwargs)
        if invocation.kind == "turn" and invocation.turn_id not in lost_ids:
            lost_ids.add(invocation.turn_id)
            raise httpx.ReadError("lost response")
        return body

    monkeypatch.setattr(dispatch, "post_signed", once)
    game = CalendarGame(config(dm_cap=1, turn_timeout_s=2), runtime_context=harness.context)
    trace = game.run_with_scenario(task())
    assert trace.metrics["total_dms_sent"] == 2 and game._remote_budget_usage[0] == 1
    assert trace.metrics["meetings_scheduled"] == 1
    assert all(event.data["attempts"] == 2 for event in trace.events
               if event.type in {"turn_end", "decide_end"} and event.data["agent_id"] == 0)


@pytest.mark.parametrize("admitted", [False, True])
def test_unavailable_registration_returns_stopped_not_completed(remote_harness, admitted):
    harness = remote_harness(join=admitted, join_timeout_s=0.05 if not admitted else 10)
    if admitted:
        def broken(invocation):
            raise ValueError("unavailable")
        harness.runtimes[0].policy.calls = broken
    trace = CalendarGame(config(), runtime_context=harness.context).run_with_scenario(task())
    assert trace.stopped and not trace.metrics
    assert [event.type for event in trace.events] == ["game_start", "game_stopped"]
    assert trace.events[-1].data["reason"] == "seat_unavailable"
    assert trace.final_state["calendars"] == task()["calendars"]
    assert not harness.episode.active and harness.episode.seats[0].secret is None


def test_late_completion_and_late_mcp_writes_have_no_effect(open_remote_turn):
    harness = open_remote_turn
    client = harness.client
    invocation = client.pending
    recorder = client.seat.recorder
    recorder.deadline = time.monotonic() + 0.15
    late = []

    async def write_late():
        await asyncio.sleep(0.25)
        async with connect(invocation.mcp["env"], invocation.capability) as session:
            result = await session.call_tool("schedule", {"meeting_id": 0, "slot": 1}, meta={"a2a/call_id": "late"})
            late.append(result.structuredContent)

    async def slow(value):
        await execute(value, [("env", "schedule", {"meeting_id": 0, "slot": 0})])
        asyncio.create_task(write_late())
        await asyncio.sleep(0.4)
        return []

    harness.runtimes[0].policy.calls = slow
    from calendar_game.agents import DecideResult
    result = client._act(DecideResult)
    assert result.tool_calls == [{"type": "schedule", "meeting_id": 0, "slot": 0}]
    snapshot = recorder.close()
    time.sleep(0.5)
    assert late[0]["code"] == "unauthorized" and recorder.close() == snapshot
    assert recorder.closed_by == "deadline" and recorder.completion is None


def test_retry_observation_matches_push_including_attempt_and_conflict(open_remote_turn):
    harness = open_remote_turn
    client = harness.client
    client.seat.recorder.close()
    client.prepare({"phase": "DECISION_RETRY", "parent_phase": "DECISION", "round": 0,
                    "turn": 1, "attempt": 1, "max_attempts": 2, "conflict": "occupied slot",
                    "calendar_snapshot_render": "seat-scoped", "prompt_sent": "retry"}, 0)
    outcomes = asyncio.run(execute(client.pending, [("env", "get_observation", {})]))
    assert outcomes[0]["data"] == client.pending.observation
    assert outcomes[0]["data"]["attempt"] == 1 and outcomes[0]["data"]["conflict"] == "occupied slot"


def test_authenticated_oversized_and_overcap_calls_emit_bounded_worker_rejections(remote_harness):
    harness = remote_harness()
    original = harness.runtimes[0].policy.calls

    def invalid(invocation):
        if invocation.phase == "CHEAP_TALK":
            recorder = harness.episode.seats[0].recorder
            recorder.max_calls = 2
            return [("comm", "send", {"channel": "dm", "to": 1, "content": "x" * 16385})] * 5
        return original(invocation)

    harness.runtimes[0].policy.calls = invalid
    game = CalendarGame(config(), runtime_context=harness.context)
    worker = threading.get_ident()
    append = game.events.append
    emitters = []

    def checked(*args, **kwargs):
        emitters.append(threading.get_ident())
        return append(*args, **kwargs)

    game.events.append = checked
    trace = game.run_with_scenario(task())
    rejected = [event for event in trace.events if event.type == "invalid_tool_call"]
    assert [event.data["tool_call"]["code"] for event in rejected] == ["invalid_arguments", "invalid_arguments", "call_cap"]
    assert game._remote_budget_usage[0] == 0 and trace.metrics["total_dms_sent"] == 1
    assert set(emitters) == {worker} and "x" * 100 not in trace.model_dump_json()


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), {"DECISION": 0}, {"unknown": 1}])
def test_invalid_phase_timeouts_fail_before_transport(timeout):
    with pytest.raises(ValueError):
        CalendarGame(config(turn_timeout_s=timeout), runtime_context=None)
