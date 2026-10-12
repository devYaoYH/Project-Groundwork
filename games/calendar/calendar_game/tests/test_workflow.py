import asyncio
import copy
import dataclasses
import inspect
import json
import threading
import time
from types import SimpleNamespace

import pytest

from a2a_engine.workflow import environment_workflow, export_program
from a2a_engine.workflow_runtime import require_agent_step
from calendar_game.agents import Agent, BaseClient, DecideResult, ReflectionResult, TurnResult
from calendar_game.calendar import Calendar
from calendar_game.game import CalendarGame
from calendar_game.tests._legacy_workflow_oracle import LegacyCalendarGame
from calendar_game.tests.test_remote_parity import normalized_events


def result(actions, decision=False):
    return (DecideResult if decision else TurnResult)(copy.deepcopy(actions), None, None, None, None, None)


class ControlledClient(BaseClient):
    def __init__(self, calls, case):
        self.calls, self.case, self.meeting, self.agent_id = calls, case, None, -1

    def record(self, method, **args):
        self.calls.append((self.agent_id, method, copy.deepcopy(args)))

    def register(self, agent_id, game_config):
        self.agent_id = agent_id
        self.record("register", game_config=dataclasses.asdict(game_config))

    def start_round(self, meeting, calendar_render, round_num):
        self.meeting = meeting
        self.record("start_round", meeting=meeting, calendar=calendar_render, round=round_num)

    def observe_calendar(self, calendar_render):
        self.record("observe_calendar", calendar=calendar_render)

    def observe_messages(self, messages):
        self.record("observe_messages", messages=messages)

    def observe_penalty(self, incurred_penalty):
        self.record("observe_penalty", penalty=incurred_penalty)

    def turn(self, messages, turn_index=None, max_turns_per_round=None):
        self.record("turn", messages=messages, turn=turn_index, cap=max_turns_per_round)
        if self.case == "long_cap":
            actions = [{"type": "dm", "to": 1 - self.agent_id, "content": "repeat"}]
        elif turn_index == 0 and self.agent_id in {0, 2}:
            actions = [{"type": "dm", "to": 2 if self.agent_id == 0 else 3, "content": "contact"}]
        else:
            actions = []
        return result(actions)

    def actions(self, retry=False):
        schedule = {"type": "schedule", "meeting_id": self.meeting["id"], "slot": 2}
        if self.case == "blocked" and self.agent_id == 0 and not retry:
            return [{"type": "reschedule", "item_id": 99, "from_slot": 0, "to_slot": 1, "justification": "move"}, schedule]
        if self.case == "empty" or (self.case == "exhausted" and self.agent_id == 1):
            return []
        if self.case == "retries" and self.agent_id == 0 and not retry:
            return [{**schedule, "slot": 999}, {"type": "dm", "to": 1, "content": "decision budget"}]
        return [schedule]

    def decide(self, meeting, calendar_render):
        self.record("decide", meeting=meeting, calendar=calendar_render)
        return result(self.actions(), True)

    def voluntary_decide(self, meeting, calendar_render):
        self.record("voluntary", meeting=meeting, calendar=calendar_render)
        actions = [{"type": "reschedule", "item_id": 7, "from_slot": 0, "to_slot": 999, "justification": "move"}] if self.case == "retries" and self.agent_id == 2 else []
        return result(actions, True)

    def retry_decide(self, attempt, max_attempts, conflict):
        self.record("retry", attempt=attempt, max_attempts=max_attempts, conflict=conflict)
        actions = ([{"type": "reschedule", "item_id": 7, "from_slot": 0, "to_slot": 1, "justification": "move"}]
                   if self.agent_id == 2 and self.case == "retries" else self.actions(True))
        return result(actions, True)

    def reflect_calendar_belief(self, target_agent_id, num_slots, round_num=None):
        self.record("reflect", target=target_agent_id, slots=num_slots, round=round_num)
        if self.agent_id == 1:
            raise ValueError("controlled reflection failure")
        return ReflectionResult(target_agent_id, [{"target_agent_id": target_agent_id, "slot": s,
            "estimate_state": 0, "probability_free": 1, "probability_busy": 0, "confidence": 1,
            "logprobs": {"0": None, "1": None}} for s in range(num_slots)], None, None, None, None)


def setup_case(case, cap=1, retries=1, frequency="round"):
    n = 2 if case == "long_cap" else 4
    cfg = {"seed": 42, "num_agents": n, "num_slots": 4, "num_meetings": 2,
           "agents": [{"type": "scripted"}] * n, "max_turns_per_round": cap,
           "decision_retries": retries, "enable_fallback": False, "enable_reflection": True,
           "reflection_frequency": frequency, "reflection_max_parallelism": 1,
           "communication": {"topology": {"default": {"graph": "complete", "channels": {"dm": {"enabled": True}},
               "budget": {"per_agent_per_round": -1 if case == "long_cap" else 2}}}}}
    calendars = [[None] * 4 for _ in range(n)]
    if case == "blocked":
        calendars[0][0] = {"errand_id": 99, "cost": 10, "blocked": True}
    if case == "retries":
        calendars[2][0] = {"errand_id": 7, "cost": 5}
    scenario = {"seed": 42, "calendars": calendars, "meetings": [
        {"id": i, "participants": [0, 1], "speaker_order": [1, 0], "duration": 1, "cost": 1}
        for i in range(2)], "prior_meetings": []}
    return cfg, scenario


def play(cls, case, cfg, scenario):
    calls, agents = [], []
    for slots in scenario["calendars"]:
        agent = Agent(ControlledClient(calls, case))
        agent.calendar = Calendar(len(slots))
        agent.calendar.slots = copy.deepcopy(slots)
        agents.append(agent)
    game = cls(copy.deepcopy(cfg))
    return game._run_with_agents(agents, copy.deepcopy(scenario)), calls


def test_calendar_producer_marks_only_invocation_wrappers():
    cfg, scenario = setup_case("retries", cap=2, retries=2)
    trace, _ = play(CalendarGame, "retries", cfg, scenario)
    wrappers = {"turn_start", "turn_end", "decide_start", "decide_end"}
    assert wrappers <= {event.type for event in trace.events}
    for event in trace.events:
        assert event.synthetic == (event.type in wrappers)
        assert "synthetic" not in event.data


@pytest.mark.parametrize("function", ["prepare_retry", "record_retry"])
def test_remote_retry_producer_marks_envelope(function, monkeypatch):
    import calendar_game.workflow_steps as steps
    from a2a_engine.tracing import EventLog

    agent = SimpleNamespace(agent_id=0, calendar=Calendar(4))
    log = EventLog()
    game = SimpleNamespace(_remote_clients={0: object()}, config=SimpleNamespace(decision_retries=2),
                           _append_event=log.append, _prompt_calendar_for_agent=lambda *args: "calendar")
    context = SimpleNamespace(game=game, total_client_calls={0: 0}, round_num=1)
    state = {"attempt": 0, "mode": "DECISION", "conflict": "conflict", "result": SimpleNamespace(retry_count=1)}
    monkeypatch.setattr(steps, "select_ordered_actor", lambda *args: agent)
    monkeypatch.setattr(steps, "decision_inbox", lambda *args: {})
    monkeypatch.setattr(steps, "event_data", lambda *args, **kwargs: kwargs)
    monkeypatch.setattr(steps, "result_data", lambda *args: {})
    asyncio.run(getattr(steps, function)(state, SimpleNamespace(context=context)))
    assert log.all()[0].synthetic is True
    assert "synthetic" not in log.all()[0].data


@pytest.mark.parametrize("case,cap,retries,frequency", [
    ("cap", 1, 1, "round"), ("cap", 2, 1, "round"), ("retries", 2, 2, "round"),
    ("blocked", 1, 1, "round"), ("exhausted", 1, 2, "round"), ("empty", 1, 0, "round"),
    ("empty", 0, 0, "round"), ("cap", 1, -1, "round"), ("cap", 2, 1, "environment"),
    ("long_cap", 40, 1, "round"),
])
def test_integrated_subgraphs_match_frozen_driver_exactly(case, cap, retries, frequency):
    cfg, scenario = setup_case(case, cap, retries, frequency)
    expected, expected_calls = play(LegacyCalendarGame, case, cfg, scenario)
    actual, calls = play(CalendarGame, case, cfg, scenario)
    assert calls == expected_calls
    assert normalized_events(actual) == normalized_events(expected)
    assert actual.final_state == expected.final_state
    assert actual.metrics == expected.metrics
    if case == "cap" and cap == 1:
        # The second snapshot activates seat 2 but must not drain seat 3 on the same pass.
        turns = [args for seat, method, args in calls if seat == 3 and method == "turn"]
        assert not turns
        assert any(seat == 3 and method == "voluntary" for seat, method, args in calls)


def test_export_is_complete_and_has_actual_retry_and_batch_boundaries_without_execution(monkeypatch):
    import calendar_game.workflow as workflow
    import calendar_game.workflow_steps as steps

    def fail(*args, **kwargs):
        raise AssertionError("export executed Calendar")

    for name, original in vars(steps).copy().items():
        if inspect.isfunction(original) and original.__module__ == steps.__name__:
            def double(*args, **kwargs):
                return fail(*args, **kwargs)
            double.__module__, double.__qualname__, double.__name__ = original.__module__, original.__qualname__, original.__name__
            monkeypatch.setattr(steps, name, double)
    for name in ("__init__", "runtime_capability", "_run_async", "generate_scenario", "_build_agents", "_append_event"):
        monkeypatch.setattr(CalendarGame, name, fail)
    for target in ("calendar_game.game.solve_optimal", "calendar_game.game.resolve_config",
                   "calendar_game.remote.RemoteSeatClient.__init__", "a2a_engine.remote.server.RuntimeManager.__init__"):
        monkeypatch.setattr(target, fail)
    first = environment_workflow("calendar")
    assert first == environment_workflow("calendar")
    assert first["status"] == "available"
    body = first["workflow"]
    assert body["diagnostics"] == []
    assert len(body["graphs"]) == 9
    assert {g["id"]: (len(g["nodes"]), len(g["edges"])) for g in body["graphs"]} == {
        "root": (6, 7),
        "root/meeting": (16, 20),
        "root/meeting/cheap_talk": (12, 15),
        "root/meeting/voluntary_decisions": (14, 18),
        "root/meeting/participant_decisions": (17, 22),
        "root/meeting/round_reflection": (5, 4),
        "root/meeting/round_reflection/bounded_fan_out": (3, 2),
        "root/environment_reflection": (5, 4),
        "root/environment_reflection/bounded_fan_out": (3, 2),
    }
    assert sum(len(g["nodes"]) for g in body["graphs"]) == 81
    root = next(g for g in body["graphs"] if g["id"] == "root")
    assert {n["id"] for n in root["nodes"]} == {
        "__start__", "next_meeting", "meeting", "environment_reflection", "finalize", "__end__",
    }
    assert {e.get("route_key") for e in root["edges"]} - {None} == {"next", "reflect", "done"}
    meeting = next(g for g in body["graphs"] if g["id"] == "root/meeting")
    assert {e.get("route_key") for e in meeting["edges"]} - {None} == {
        "invoke", "finished", "eligible", "suppressed", "success", "depth_exceeded", "no_feasible_slot", "reflect", "skip",
    }
    assert {n.get("operation") for n in meeting["nodes"]} - {None} == {"start_round"}
    operations = {n.get("operation") for g in body["graphs"] for n in g["nodes"]}
    assert not operations & {"register", "episode_end"}
    assert not {n["id"] for n in root["nodes"]} & {"initialize", "external_readiness", "stopped_trace"}
    assert not {n["id"] for n in root["nodes"]} & {
        "voluntary_mode", "participant_mode", "prepare_round_reflection", "collect_round_reflections",
        "prepare_environment_reflection", "collect_environment_reflections",
    }
    talk = next(g for g in body["graphs"] if g["id"] == "root/meeting/cheap_talk")
    assert {e.get("route_key") for e in talk["edges"] if e["source"] == "deliver_talk"} == {"active", "contacted"}
    voluntary = next(g for g in body["graphs"] if g["id"] == "root/meeting/voluntary_decisions")
    assert {e.get("route_key") for e in voluntary["edges"]} - {None} == {
        "invoke", "finished", "validate", "skip", "apply", "retry", "exhausted",
    }
    assert "apply_voluntary" in {n["id"] for n in voluntary["nodes"]}
    assert not {n["id"] for n in voluntary["nodes"]} & {
        "participant_call", "stage_participant", "transaction", "commit_participants", "rollback_participants",
    }
    decision = next(g for g in body["graphs"] if g["id"] == "root/meeting/participant_decisions")
    assert {e.get("route_key") for e in decision["edges"]} - {None} == {
        "invoke", "finished", "validate", "skip", "stage", "retry", "exhausted", "commit", "rollback",
    }
    assert {n["id"] for n in decision["nodes"]} >= {"stage_participant", "transaction", "commit_participants", "rollback_participants"}
    assert not {n["id"] for n in decision["nodes"]} & {"voluntary_call", "apply_voluntary"}
    for child in (voluntary, decision):
        assert {n.get("operation") for n in child["nodes"]} - {None} == {
            "voluntary_decide" if child is voluntary else "decide", "retry_decide",
        }
        for source in ("record_decision_actions", "record_retry_actions"):
            assert {e.get("route_key") for e in child["edges"] if e["source"] == source} == {"validate", "skip"}
    assert not any(node["kind"] == "batch" for node in decision["nodes"])
    reflection = next(g for g in body["graphs"] if g["id"] == "root/meeting/round_reflection")
    batch = next(node for node in reflection["nodes"] if node["kind"] == "batch")
    assert batch["serial_key_selector"] == "calendar_game.workflow_steps.remote_reporter_serial_key"
    for child in (reflection, next(g for g in body["graphs"] if g["id"] == "root/environment_reflection")):
        assert {n["id"] for n in child["nodes"]} == {"__start__", "launch", "bounded_fan_out", "join_in_call_order", "__end__"}
    body_call = next(g for g in body["graphs"] if g["id"] == batch["subgraph"])
    assert next(n for n in body_call["nodes"] if n["kind"] == "agent_call")["actor_selector"].endswith("select_reporter_target")
    assert len(json.dumps(first)) < 80000
    assert all(not n.get("source_ref", {}).get("path", "").startswith("/") for g in body["graphs"] for n in g["nodes"])


@pytest.mark.parametrize("outcome", ["success", "depth_exceeded", "no_feasible_slot", "dry_run", "blocked", "zero_meetings"])
def test_complete_episode_fallback_and_exit_routes_match_frozen_driver(outcome, monkeypatch):
    from calendar_game.fallback import FallbackDepthExceeded, FallbackImpossible
    cfg, scenario = setup_case("empty", cap=0, retries=0)
    cfg.update(enable_fallback=True, enable_reflection=False)
    if outcome == "zero_meetings":
        scenario["meetings"] = []
    if outcome == "blocked":
        scenario["calendars"][0][0] = {"errand_id": 99, "cost": 10, "blocked": True}
    if outcome in {"depth_exceeded", "no_feasible_slot"}:
        def search(agents, meeting, *args, **kwargs):
            if outcome == "depth_exceeded":
                raise FallbackDepthExceeded(meeting["id"], 2)
            raise FallbackImpossible(meeting["id"])
        for module in ("calendar_game.game", "calendar_game.tests._legacy_workflow_oracle"):
            monkeypatch.setattr(module + ".find_fallback_slot", search)
    traces, recorded = [], []
    for cls in (LegacyCalendarGame, CalendarGame):
        calls, agents = [], []
        for slots in scenario["calendars"]:
            agent = Agent(ControlledClient(calls, "blocked" if outcome == "blocked" else "empty"))
            agent.calendar = Calendar(len(slots))
            agent.calendar.slots = copy.deepcopy(slots)
            agents.append(agent)
        game = cls(copy.deepcopy(cfg), dry_run=outcome == "dry_run")
        traces.append(game._run_with_agents(agents, copy.deepcopy(scenario)))
        recorded.append(calls)
    expected, actual = traces
    assert recorded[0] == recorded[1]
    assert normalized_events(expected) == normalized_events(actual)
    assert expected.final_state == actual.final_state
    assert expected.metrics == actual.metrics
    errors = [event.data["reason"] for event in actual.events if event.type == "fallback_error"]
    if outcome in {"depth_exceeded", "no_feasible_slot"}:
        assert errors == [outcome] * 2
    if outcome == "success":
        assert sum(event.type == "fallback_applied" for event in actual.events) == 2
    if outcome in {"dry_run", "blocked", "zero_meetings"}:
        assert not any(event.type == "fallback_start" for event in actual.events)


@pytest.mark.parametrize("parallelism", [1, 2, None])
def test_delayed_local_reflection_launch_join_order_and_concurrency(parallelism):
    game = CalendarGame({"num_agents": 3, "num_slots": 1, "reflection_max_parallelism": parallelism})
    log, active, peak = [], 0, 0
    lock, second_finished = threading.Lock(), threading.Event()
    append = game._append_event

    def event(name, **kwargs):
        log.append((name, kwargs["data"].get("target_agent_id")))
        append(name, **kwargs)
    game._append_event = event

    class Delayed(ControlledClient):
        def reflect_calendar_belief(self, target_agent_id, num_slots, round_num=None):
            nonlocal active, peak
            require_agent_step()
            assert len([entry for entry in log if entry[0] == "reflection_start"]) == 2
            with lock:
                active += 1
                peak = max(peak, active)
            if parallelism != 1 and target_agent_id == 1:
                assert second_finished.wait(2)
            time.sleep(0.02)
            with lock:
                active -= 1
                log.append(("finished", target_agent_id))
            if target_agent_id == 2:
                second_finished.set()
            return ReflectionResult(target_agent_id, [{"target_agent_id": target_agent_id, "slot": 0}], None, None, None, None)

    agents = [Agent(Delayed([], "cap")) for _ in range(3)]
    estimates, counts = asyncio.run(game._run_reflection_measurement(agents=agents, all_agent_ids=[0, 1, 2],
        round_num=0, event_round=0, reporter_agent_ids=[0]))
    assert peak == (1 if parallelism == 1 else 2)
    assert [target for kind, target in log if kind == "finished"] == ([1, 2] if parallelism == 1 else [2, 1])
    assert [target for kind, target in log if kind == "reflection_end"] == [1, 2]
    assert [row["target_agent_id"] for row in estimates] == [1, 2]
    assert counts == {0: 2}


@pytest.mark.parametrize("parallelism", [1, 2])
def test_queued_remote_reflection_keeps_launch_deadline(parallelism):
    game = CalendarGame({"num_agents": 3, "num_slots": 1, "reflection_max_parallelism": parallelism})
    dispatches, deadlines = [], []

    class RemoteDouble(ControlledClient):
        def prepare_reflection(self, target, slots, round_num):
            deadline = time.monotonic() + 0.08
            deadlines.append(deadline)
            return (target, deadline)

        def reflect_calendar_belief(self, target_agent_id, num_slots, round_num=None, *, prepared=None):
            require_agent_step()
            assert prepared[0] == target_agent_id
            if time.monotonic() >= prepared[1]:
                return None
            dispatches.append(target_agent_id)
            time.sleep(0.12)
            return ReflectionResult(target_agent_id, [], None, None, None, None)

    client = RemoteDouble([], "cap")
    game._remote_clients[0] = client
    agents = [Agent(client), Agent(client), Agent(client)]
    estimates, counts = asyncio.run(game._run_reflection_measurement(agents=agents, all_agent_ids=[0, 1, 2],
        round_num=0, event_round=0, reporter_agent_ids=[0]))
    assert dispatches == [1] and len(deadlines) == 2
    assert abs(deadlines[0] - deadlines[1]) < 0.02
    assert counts == {0: 1}
    assert not estimates
