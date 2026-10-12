import asyncio
import copy
import json
import time

import httpx
import pytest

from a2a_agent.mcp_client import execute
from calendar_game.agents import GameConfig
from calendar_game.game import CalendarGame
from calendar_game.remote import RemoteSeatClient
from calendar_game.tests.test_remote_parity import normalized_events, config, scenario


@pytest.mark.parametrize("seats", [(0,), (1,), (0, 1)])
@pytest.mark.parametrize("graph", ["complete", "ring", "star", "edges"])
def test_v2_full_calendar_staging_matches_local_and_legacy(remote_harness_v2, remote_harness, seats, graph):
    default = {"graph": graph, "channels": {"dm": {"enabled": True}}, "budget": {"per_agent_per_round": 1}}
    if graph == "star":
        default["hub"] = 0
    elif graph == "edges":
        default.update(edges=[[0, 1]], directed=True)
    cfg = config(communication={"topology": {"default": default}})
    local = CalendarGame(cfg).run_with_scenario(copy.deepcopy(scenario()))
    traces = []
    snapshots = []
    for factory in (remote_harness, remote_harness_v2):
        harness = factory(seats=seats)
        mixed = copy.deepcopy(cfg)
        for seat in seats:
            mixed["agents"][seat]["runtime"] = "external"
        remote = CalendarGame(mixed, runtime_context=harness.context).run_with_scenario(copy.deepcopy(scenario()))
        assert normalized_events(remote) == normalized_events(local)
        assert remote.final_state == local.final_state
        assert remote.metrics == local.metrics
        snapshots.append([s.recorder.snapshot for s in harness.episode.seats.values()])
        traces.append(remote)
        for item in harness.invocations:
            assert item.protocol_version == ("a2a-turns/2" if factory == remote_harness_v2 else "a2a-turns/1")
        assert not harness.episode.active
        for event in remote.events:
            if event.type == "agent_registered" and event.data["agent_id"] in seats:
                assert event.data["protocol_version"] == ("a2a-turns/2" if factory == remote_harness_v2 else "a2a-turns/1")
    assert snapshots[0] == snapshots[1]


def test_v2_lost_acknowledgement_completion_takes_precedence(remote_harness_v2, monkeypatch):
    from a2a_engine.remote import network
    harness = remote_harness_v2()
    original = network.mcp_call
    lost = []

    async def lose(url, credential, name, arguments, **kwargs):
        response = await original(url, credential, name, arguments, **kwargs)
        if name == "start_turn":
            lost.append(arguments["turn_id"])
            raise httpx.ReadError("lost acknowledgement")
        return response

    monkeypatch.setattr(network, "mcp_call", lose)
    cfg = config(agents=[{"type": "scripted", "runtime": "external"}, {"type": "scripted"}])
    trace = CalendarGame(cfg, runtime_context=harness.context).run_with_scenario(scenario())
    assert not trace.stopped and trace.metrics["meetings_scheduled"] == 1
    assert lost
    assert all(r.closed_by == "completed" for r in harness.environment.registry.lifecycle.records.values())


@pytest.mark.parametrize("staged", [False, True])
def test_v2_expiry_retains_only_prior_staging_and_cancels_work(remote_harness_v2, staged):
    harness = remote_harness_v2()
    original = harness.policies[0].calls
    cancelled = []

    async def slow(value):
        if value.phase != "DECISION":
            return original(value)
        if staged:
            await execute(value, [("env", "schedule", {"meeting_id": 0, "slot": 0})])
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)
        return []

    harness.policies[0].calls = slow
    cfg = config(agents=[{"type": "scripted", "runtime": "external"}, {"type": "scripted"}],
                 turn_timeout_s={"DECISION": 0.3, "register": 1, "round_start": 1, "episode_end": 0.3})
    trace = CalendarGame(cfg, runtime_context=harness.context).run_with_scenario(scenario())
    assert trace.metrics["meetings_scheduled"] == int(staged)
    end = next(e for e in trace.events if e.type == "decide_end" and e.data["agent_id"] == 0)
    assert end.data["closed_by"] == "deadline"
    assert cancelled == [True]


def test_v2_acknowledgement_never_completes_recorder_and_late_calls_fail(remote_harness_v2):
    from a2a_engine.remote.contract import TurnFinished
    from a2a_engine.remote.network import mcp_call
    harness = remote_harness_v2()
    game = CalendarGame(config())
    client = RemoteSeatClient(harness.context, 0, game.router)
    harness.register_client(client, GameConfig(2, 3, 0, [0, 1]))
    client.start_round(scenario()["meetings"][0], "Slot 0: [FREE]", 0)
    client.prepare({"phase": "DECISION", "round": 0, "turn": 0, "prompt_sent": "decide",
                    "calendar_snapshot_render": "Slot 0: [FREE]"}, 0)
    invocation = client.pending
    recorder = client.seat.recorder
    record = harness.environment.registry.lifecycle.open(invocation, deadline=time.monotonic() + 10, recorder=recorder)
    harness.policies[0].calls = lambda value: asyncio.sleep(10, result=[])
    acceptance = harness.io.run(client.dispatcher._start(client.seat, invocation, record.deadline, record))
    assert acceptance.status == "accepted" and not recorder.closed and recorder.completion is None
    finished = TurnFinished(**invocation.identity().model_dump(), status="succeeded")
    result = asyncio.run(mcp_call(invocation.mcp["lifecycle"], invocation.lifecycle_credential,
                                  "complete_turn", finished.model_dump(mode="json")))
    assert result["disposition"] == "recorded"
    assert recorder.closed_by == "completed"
    assert asyncio.run(mcp_call(invocation.mcp["lifecycle"], invocation.lifecycle_credential,
                               "complete_turn", finished.model_dump(mode="json")))["disposition"] == "duplicate"
    outcomes = asyncio.run(execute(invocation, [("env", "schedule", {"meeting_id": 0, "slot": 2})]))
    assert outcomes[0]["code"] == "unauthorized" and not recorder.snapshot


def test_client_constructed_before_admission_selects_negotiated_dispatcher(remote_harness_v2):
    from a2a_engine.remote.dispatch import TurnDispatcher, TurnDispatcherV2
    harness = remote_harness_v2(join=False)
    client = RemoteSeatClient(harness.context, 0, CalendarGame(config()).router)
    assert isinstance(client.dispatcher, TurnDispatcher)
    harness.callback.run(harness.runtime.join(f"{harness.io.base_url}/episodes/episode/join", harness.callback.base_url,
                                              harness.episode.seats[0].ticket, policy=harness.policies[0]))
    harness.register_client(client, GameConfig(2, 3, 0, [0, 1]), select_dispatcher=False)
    assert isinstance(client.dispatcher, TurnDispatcher)
    client.start_round(scenario()["meetings"][0], "Slot 0: [FREE]", 0)
    assert isinstance(client.dispatcher, TurnDispatcherV2)
    assert harness.invocations[0].protocol_version == "a2a-turns/2"


def test_completion_before_start_acknowledgement_wakes_pre_registered_waiter(remote_harness_v2, monkeypatch):
    from a2a_engine.remote import network
    harness = remote_harness_v2()
    original = network.mcp_call

    async def delay_ack(url, credential, name, arguments, **kwargs):
        response = await original(url, credential, name, arguments, **kwargs)
        if name == "start_turn":
            await asyncio.sleep(10)
        return response

    monkeypatch.setattr(network, "mcp_call", delay_ack)
    client = RemoteSeatClient(harness.context, 0, CalendarGame(config()).router, turn_timeout_s=1)
    harness.register_client(client, GameConfig(2, 3, 0, [0, 1]))
    record = next(iter(harness.environment.registry.lifecycle.records.values()))
    assert record.finished.status == "succeeded" and record.acceptance is None
    assert record.event.is_set() and client.seat.registered


def test_lifecycle_receipts_remain_scoped_after_episode_teardown(remote_harness_v2):
    from a2a_engine.remote.network import mcp_call
    harness = remote_harness_v2()
    client = RemoteSeatClient(harness.context, 0, CalendarGame(config()).router)
    harness.register_client(client, GameConfig(2, 3, 0, [0, 1]))
    record = next(iter(harness.environment.registry.lifecycle.records.values()))
    value = record.invocation
    harness.environment.registry.unregister(harness.episode)
    duplicate = asyncio.run(mcp_call(value.mcp["lifecycle"], value.lifecycle_credential,
                                     "complete_turn", record.finished.model_dump(mode="json")))
    assert duplicate["disposition"] == "duplicate"
    with pytest.raises(ValueError, match="rejected"):
        asyncio.run(mcp_call(value.mcp["lifecycle"], value.lifecycle_credential,
                             "complete_turn", record.finished.model_copy(update={"seat": 1}).model_dump(mode="json")))


@pytest.mark.parametrize("admitted", [False, True])
def test_v2_unavailable_registration_fails_attempt(remote_harness_v2, admitted):
    harness = remote_harness_v2(join=admitted, join_timeout_s=0.05 if not admitted else 10)
    if admitted:
        def broken(value):
            raise ValueError("never expose this failure")
        harness.policies[0].calls = broken
    cfg = config(agents=[{"type": "scripted", "runtime": "external"}, {"type": "scripted"}])
    from a2a_engine.remote.lobby import SeatsUnavailable
    game = CalendarGame(cfg, runtime_context=harness.context)
    task = scenario()
    before = copy.deepcopy(task["calendars"])
    with pytest.raises(SeatsUnavailable, match="seat_unavailable") as error:
        game.run_with_scenario(task)
    assert error.value.seats == (0,) and "never expose" not in str(error.value)
    assert [event.type for event in game.events.all()] == ["game_start"]
    assert task["calendars"] == before and not harness.episode.active


def test_v2_mock_provider_keeps_prompts_history_telemetry_and_reflection_separate(remote_harness_v2):
    from a2a_agent.strategies import StructuredOutputStrategy
    harness = remote_harness_v2()

    class MockModel:
        def __init__(self):
            self.requests = []

        def streaming_with_retry(self, messages, **options):
            self.requests.append((copy.deepcopy(messages), options))
            text = (json.dumps({"actions": [{"type": "schedule", "meeting_id": 0, "slot": 0}],
                                "thinking": "agent-private"}) if len(self.requests) == 1 else '{"deltas":[-3,0,3]}')
            return {"text": text, "prompt_tokens": 7, "completion_tokens": 3, "duration_s": 0.1,
                    "_raw_response": {"secret": "never-persist-raw"}}

    strategy = StructuredOutputStrategy(MockModel())
    next(iter(harness.runtime.sessions.values())).policy = strategy
    client = RemoteSeatClient(harness.context, 0, CalendarGame(config()).router)
    harness.register_client(client, GameConfig(2, 3, 0, [0, 1]))
    client.start_round(scenario()["meetings"][0], "Slot 0: [FREE]", 0)
    client.prepare({"phase": "DECISION", "round": 0, "turn": 0, "prompt_sent": "verbatim decision prompt",
                    "calendar_snapshot_render": "Slot 0: [FREE]"}, 0)
    result = client.decide({}, "")
    assert result.tool_calls == [{"type": "schedule", "meeting_id": 0, "slot": 0}]
    assert result.usage.total_tokens == 10 and result.latency_ms == 100 and result.raw is None
    assert strategy.model.requests[0][0][-1] == {"role": "user", "content": "verbatim decision prompt"}
    assert len(strategy.history) == 2
    before = copy.deepcopy(strategy.history)
    reflected = client.reflect_calendar_belief(1, 3, round_num=0)
    assert strategy.history == before and reflected is not None
    assert [entry["belief_delta_occupied"] for entry in reflected.estimates] == [-3, 0, 3]
    assert strategy.model.requests[1][1]["temperature"] == 0
    assert all(0 < options["timeout"] <= 120 and options["max_retries"] == 1 for _, options in strategy.model.requests)
    terminal_bodies = "".join(record.finished.model_dump_json() for record in harness.runtime.records.values())
    assert "agent-private" not in terminal_bodies and "never-persist-raw" not in terminal_bodies
