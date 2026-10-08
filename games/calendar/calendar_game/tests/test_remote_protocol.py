import copy
import os
import subprocess
import sys
import threading

import pytest

from a2a_engine.remote.dispatch import LoopbackServer
from a2a_engine.remote.server import EnvironmentApp
from calendar_game.game import CalendarGame
from calendar_game.remote import CALENDAR_TOOLS, CalendarRuntimeContext


def config(**overrides):
    return {"seed": 42, "num_agents": 2, "num_slots": 3, "num_meetings": 1,
            "density": 0, "decision_retries": 0, "enable_reflection": False,
            "enable_fallback": False, "max_turns_per_round": 3,
            "agents": [{"type": "scripted"}] * 2, **overrides}


def scenario():
    return {"seed": 42, "calendars": [[None] * 3, [None] * 3],
            "meetings": [{"id": 0, "participants": [0, 1], "speaker_order": [0, 1], "duration": 1, "cost": 1}]}


LIFECYCLE_FIELDS = {"runtime", "protocol_version", "agent_info", "harness", "turn_id", "deadline", "closed_by", "attempts", "telemetry_source"}


def normalized_events(trace):
    return [(event.type, {key: value for key, value in event.data.items() if key not in LIFECYCLE_FIELDS})
            for event in trace.events]


@pytest.mark.parametrize("remote_seat", [0, 1])
@pytest.mark.parametrize("protocol,cap", [("dm", 100), ("participant_groupchat", 100), ("all_groupchat", 100), ("dm", 0)])
def test_complete_mixed_episode_matches_all_local(remote_harness, remote_seat, protocol, cap):
    cfg = config(communication_protocol=protocol, dm_cap=cap)
    local = CalendarGame(cfg).run_with_scenario(copy.deepcopy(scenario()))
    harness = remote_harness(seats=(remote_seat,))
    authored = copy.deepcopy(cfg)
    authored["agents"][remote_seat] = {"type": "scripted", "runtime": "external"}
    ticket = harness.episode.seats[remote_seat].ticket
    secret = harness.episode.seats[remote_seat].secret
    game = CalendarGame(authored, runtime_context=harness.context)
    trace = game.run_with_scenario(copy.deepcopy(scenario()))
    assert normalized_events(trace) == normalized_events(local)
    assert trace.final_state == local.final_state
    assert trace.metrics == local.metrics
    assert trace.metrics["meetings_scheduled"] == 1
    assert [event.type for event in trace.events][0] == "game_start"
    assert [event.type for event in trace.events][-1] == "game_end"
    assert all(not invocation.capability for invocation in harness.invocations if invocation.kind != "turn")
    assert harness.invocations[0].kind == "register"
    assert harness.invocations[-1].kind == "episode_end"
    assert len({invocation.turn_id for invocation in harness.invocations}) == len(harness.invocations)
    starts = [event for event in trace.events if event.type in {"turn_start", "decide_start"} and event.data["agent_id"] == remote_seat]
    actions = [invocation for invocation in harness.invocations if invocation.kind == "turn"]
    assert len(starts) == len(actions)
    for event, invocation in zip(starts, actions, strict=True):
        assert event.data["turn_id"] == invocation.turn_id
        assert event.data["prompt_sent"] == invocation.prompt
        assert event.data.get("inbox_drained", []) == invocation.inbox
        assert set(invocation.observation) == {"meeting", "round", "turn_index", "calendar_render", "incurred_penalty"}
    assert not harness.episode.active
    assert harness.runtimes[remote_seat].ended.is_set()
    persisted = trace.model_dump_json()
    assert ticket not in persisted and secret not in persisted
    assert all(invocation.capability not in persisted for invocation in actions)
    assert authored == {**cfg, "agents": [{"type": "scripted", "runtime": "external"} if seat == remote_seat else {"type": "scripted"} for seat in range(2)]}


def test_complete_episode_with_independently_started_process():
    environment = EnvironmentApp(CALENDAR_TOOLS)
    with LoopbackServer(environment.app) as io:
        episode = environment.registry.provision("subprocess", (1,), join_timeout_s=15)
        ticket = episode.seats[1].ticket
        env = {**os.environ, "A2A_JOIN_TICKET": episode.seats[1].ticket}
        process = subprocess.Popen([sys.executable, "-m", "a2a_agent.server", "--join-url",
                                    f"{io.base_url}/episodes/subprocess/join"], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            episode.wait_ready(1)
            cfg = config(agents=[{"type": "scripted"}, {"type": "scripted", "runtime": "external"}])
            game = CalendarGame(cfg, runtime_context=CalendarRuntimeContext(environment, io, episode))
            trace = game.run_with_scenario(scenario())
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr
            local = CalendarGame(config()).run_with_scenario(scenario())
            assert normalized_events(trace) == normalized_events(local)
            assert trace.final_state == local.final_state
            assert trace.metrics == local.metrics
            assert ticket not in stdout + stderr
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)
            environment.registry.unregister(episode)


@pytest.mark.parametrize("overrides", [{"enable_reflection": True}, {"decision_retries": 1}])
def test_full_remote_features_are_enabled(remote_harness, overrides):
    harness = remote_harness()
    trace = CalendarGame(config(agents=[{"type": "scripted", "runtime": "external"}, {"type": "scripted"}], **overrides),
                         runtime_context=harness.context).run_with_scenario(scenario())
    assert trace.metrics["meetings_scheduled"] == 1
    if overrides.get("enable_reflection"):
        assert any(event.type == "reflection_end" and event.data["agent_id"] == 0 for event in trace.events)


@pytest.mark.parametrize("runtime", ["human", "unknown"])
def test_unimplemented_runtimes_fail_before_build(runtime):
    with pytest.raises(ValueError, match="not yet supported|unknown runtime"):
        CalendarGame(config(agents=[{"type": "scripted", "runtime": runtime}, {"type": "scripted"}]))


def test_remote_configuration_requires_private_provisioning_output(monkeypatch):
    monkeypatch.delenv("A2A_PROVISIONING_DIR", raising=False)
    game = CalendarGame(config(agents=[{"type": "scripted", "runtime": "external"}, {"type": "scripted"}]))
    with pytest.raises(ValueError, match="private provisioning_dir"):
        game.run()
    assert not game.runtime_manager.io.thread.is_alive()


def test_remote_nonparticipant_and_teardown(remote_harness):
    harness = remote_harness()
    game = CalendarGame(config(agents=[{"type": "scripted", "runtime": "external"}, {"type": "scripted"}]),
                        runtime_context=harness.context)
    task = scenario()
    task["meetings"][0]["participants"] = [1]
    task["meetings"][0]["speaker_order"] = [1]
    trace = game.run_with_scenario(task)
    assert trace.metrics["meetings_scheduled"] == 1
    assert not harness.episode.active
    assert harness.episode.seats[0].secret is None


def test_worker_emits_schema_rejection_once_without_state_mutation(remote_harness):
    harness = remote_harness()
    runtime = harness.runtimes[0]
    original = runtime.policy.calls

    def invalid(invocation):
        if invocation.kind == "turn" and invocation.phase == "CHEAP_TALK":
            return [("env", "reschedule", {"item_id": 10, "from_slot": 0, "to_slot": 1, "justification": "not supported"})]
        return original(invocation)

    runtime.policy.calls = invalid
    game = CalendarGame(config(agents=[{"type": "scripted", "runtime": "external"}, {"type": "scripted"}]),
                        runtime_context=harness.context)
    worker = threading.get_ident()
    emit = game.events.append
    emit_threads = []

    def checked(event_type, **kwargs):
        emit_threads.append(threading.get_ident())
        return emit(event_type, **kwargs)

    game.events.append = checked
    trace = game.run_with_scenario(scenario())
    rejected = [event for event in trace.events if event.type == "invalid_tool_call"]
    assert len(rejected) == 2
    assert all(event.data["tool_call"]["code"] == "wrong_phase" for event in rejected)
    assert set(emit_threads) == {worker}
    assert trace.metrics["meetings_scheduled"] == 1
    assert all(calendar[0]["meeting_id"] == 0 and calendar[1:] == [None, None] for calendar in trace.final_state["calendars"])


@pytest.mark.parametrize("phase", ["CHEAP_TALK", "DECISION"])
def test_worker_preserves_cross_endpoint_attempt_order(remote_harness, phase):
    harness = remote_harness()
    original = harness.runtimes[0].policy.calls

    def actions(invocation):
        calls = original(invocation)
        if invocation.kind == "turn" and invocation.phase == phase and invocation.observation["turn_index"] == (0 if phase == "CHEAP_TALK" else 2):
            return [("comm", "send", {"channel": "dm", "to": 1, "content": "first"}),
                    ("env", "reschedule", {"item_id": 10, "from_slot": 0, "to_slot": 1}),
                    ("comm", "send", {"channel": "dm", "to": 1, "content": "last"}), *calls]
        return calls

    harness.runtimes[0].policy.calls = actions
    topology = {"default": {"graph": "complete", "channels": {"dm": {"enabled": True}}},
                "phases": {"DECISION": {"channels": {"dm": {"enabled": True}}}}}
    cfg = config(communication={"topology": topology},
                 agents=[{"type": "scripted", "runtime": "external"}, {"type": "scripted"}])
    trace = CalendarGame(cfg, runtime_context=harness.context).run_with_scenario(scenario())
    attempts = [event for event in trace.events if event.type in {"dm_sent", "invalid_tool_call"}
                and event.data["agent_id"] == 0 and event.data["phase"] == phase]
    assert [event.type for event in attempts[:3]] == ["dm_sent", "invalid_tool_call", "dm_sent"]
    assert attempts[0].data["content"] == "first" and attempts[2].data["content"] == "last"
    assert trace.metrics["meetings_scheduled"] == 1


@pytest.mark.parametrize("graph", ["complete", "ring", "star", "edges"])
def test_explicit_topology_remote_parity(remote_harness, graph):
    default = {"graph": graph, "channels": {"dm": {"enabled": True}}, "budget": {"per_agent_per_round": 1}}
    if graph == "star":
        default["hub"] = 0
    elif graph == "edges":
        default.update(edges=[[0, 1]], directed=True)
    cfg = config(communication={"topology": {"default": default}})
    local = CalendarGame(cfg).run_with_scenario(scenario())
    harness = remote_harness()
    mixed = copy.deepcopy(cfg)
    mixed["agents"][0] = {"type": "scripted", "runtime": "external"}
    remote = CalendarGame(mixed, runtime_context=harness.context).run_with_scenario(scenario())
    assert normalized_events(remote) == normalized_events(local)
    assert remote.final_state == local.final_state
    assert remote.metrics == local.metrics


def test_dry_run_keeps_external_runtime(remote_harness):
    harness = remote_harness()
    cfg = config(agents=[{"type": "llm", "runtime": "external"}, {"type": "scripted"}])
    trace = CalendarGame(cfg, dry_run=True, runtime_context=harness.context).run_with_scenario(scenario())
    assert trace.metrics["meetings_scheduled"] == 1
    assert harness.invocations[0].kind == "register"
    assert harness.invocations[-1].kind == "episode_end"


def test_context_cannot_silently_override_in_process_roster(remote_harness):
    harness = remote_harness()
    with pytest.raises(ValueError, match="do not match configured seats"):
        CalendarGame(config(), runtime_context=harness.context)


def test_scenario_generation_error_revokes_provisioning(remote_harness):
    harness = remote_harness()
    cfg = config(task_path="missing-task-file.jsonl", task_id="missing",
                 agents=[{"type": "scripted", "runtime": "external"}, {"type": "scripted"}])
    game = CalendarGame(cfg, runtime_context=harness.context)
    with pytest.raises(FileNotFoundError):
        game.run()
    assert not harness.episode.active
    assert harness.episode.seats[0].secret is None


def schedule(slot=0):
    return {"type": "schedule", "meeting_id": 0, "slot": slot}


def move(item, source, target):
    return {"type": "reschedule", "item_id": item, "from_slot": source, "to_slot": target, "justification": "coordination"}


def run_planned(remote_harness, monkeypatch, task, plan, *, seat=0, cfg=None):
    from calendar_game.agents import DecideResult
    from calendar_game.clients.scripted import ScriptedClient
    import calendar_game.game as module

    cfg = cfg or config(decision_retries=1)

    class PlannedClient(ScriptedClient):
        def _result(self, phase, attempt=0):
            tools = copy.deepcopy(plan.get((self.agent_id, phase, attempt), []))
            for tool in tools:
                if tool["type"] in {"dm", "participant_groupchat", "all_groupchat"}:
                    tool.setdefault("meeting_id", self.meeting["id"])
            return DecideResult(tools, None, None, None, None, None, retry_count=attempt)

        def turn(self, *args, **kwargs):
            result = self._result("CHEAP_TALK") if not self._turned else self._result("PASS")
            self._turned = True
            return result

        def decide(self, *args):
            self.parent = "DECISION"
            return self._result(self.parent)

        def voluntary_decide(self, *args):
            self.parent = "VOLUNTARY"
            return self._result(self.parent)

        def retry_decide(self, attempt, max_attempts, conflict):
            return self._result(self.parent, attempt)

    monkeypatch.setattr(module, "ScriptedClient", PlannedClient)
    local = CalendarGame(cfg).run_with_scenario(copy.deepcopy(task))
    harness = remote_harness(seats=(seat,))
    original = harness.runtimes[seat].policy.calls

    def calls(invocation):
        if invocation.kind != "turn":
            return original(invocation)
        harness.invocations.append(invocation)
        phase = invocation.parent_phase or invocation.phase
        tools = plan.get((seat, phase, invocation.observation.get("attempt", 0)), [])
        result = []
        for tool in tools:
            if tool["type"] in {"dm", "participant_groupchat", "all_groupchat"}:
                result.append(("comm", "send", {"channel": tool["type"], "content": tool["content"],
                                                  **({"to": tool["to"]} if "to" in tool else {})}))
            else:
                result.append(("env", tool["type"], {key: value for key, value in tool.items() if key != "type"}))
        return result

    harness.runtimes[seat].policy.calls = calls
    mixed = copy.deepcopy(cfg)
    mixed["agents"][seat] = {**mixed["agents"][seat], "runtime": "external"}
    trace = CalendarGame(mixed, runtime_context=harness.context).run_with_scenario(copy.deepcopy(task))
    return local, trace, harness


@pytest.mark.parametrize("case", ["multi_move", "retry_valid", "retry_invalid", "blocked", "empty_yield"])
def test_full_cells_and_participant_retries_preserve_authoritative_rules(remote_harness, monkeypatch, case):
    task = scenario()
    plan = {(1, "DECISION", 0): [schedule()]}
    if case == "multi_move":
        task["calendars"][0] = [{"errand_id": 1, "cost": 2}, {"errand_id": 2, "cost": 3}, None]
        plan[(0, "DECISION", 0)] = [schedule(), move(1, 0, 1), move(2, 1, 2)]
    elif case == "blocked":
        task["calendars"][0][1] = {"errand_id": 1, "cost": 1, "blocked": True}
        plan[(0, "DECISION", 0)] = [schedule(), move(1, 1, 2)]
        plan[(0, "DECISION", 1)] = [schedule()]
    elif case == "retry_valid":
        plan[(0, "DECISION", 0)] = [schedule(99)]
        plan[(0, "DECISION", 1)] = [schedule()]
    elif case == "empty_yield":
        plan[(0, "DECISION", 1)] = [schedule()]
    else:
        plan[(0, "DECISION", 0)] = [schedule(99)]
        plan[(0, "DECISION", 1)] = [schedule(99)]
    local, trace, harness = run_planned(remote_harness, monkeypatch, task, plan)
    # Retry lifecycle events are additive, but all game effects are compared exactly.
    remote_effects = [event for event in trace.events if event.data.get("phase") != "DECISION_RETRY"]
    assert normalized_events(trace.model_copy(update={"events": remote_effects})) == normalized_events(local)
    assert trace.final_state == local.final_state and trace.metrics == local.metrics
    assert trace.metrics["meetings_scheduled"] == (0 if case in {"retry_invalid", "blocked"} else 1)
    if case == "multi_move":
        assert [item.get("errand_id") for item in trace.final_state["calendars"][0][1:]] == [1, 2]
        assert len([event for event in trace.events if event.type == "decide_end" and event.data["agent_id"] == 0]) == 1
    else:
        retries = [invocation for invocation in harness.invocations if invocation.phase == "DECISION_RETRY"]
        assert len(retries) == 1 and retries[0].parent_phase == "DECISION"
        assert retries[0].observation["attempt"] == 1 and retries[0].observation["conflict"]
        starts = [event for event in trace.events if event.type == "decide_start" and event.data["agent_id"] == 0]
        assert starts[0].data["turn_id"] != starts[1].data["turn_id"]
    if case in {"retry_invalid", "blocked"}:
        assert any(event.type == "cell_rolled_back" for event in trace.events)


@pytest.mark.parametrize("retry", [False, True])
def test_nonparticipant_voluntary_moves_and_retry_keep_parent_authority(remote_harness, monkeypatch, retry):
    task = scenario()
    task["calendars"] = [[{"meeting_id": 100, "cost": 2}, None, None], [None] * 3,
                          [{"meeting_id": 100, "cost": 2}, None, None]]
    task["prior_meetings"] = [{"id": 100, "participants": [0, 2], "slot": 0, "cost": 2, "duration": 1}]
    cfg = config(num_agents=3, agents=[{"type": "scripted"} for _ in range(3)], max_turns_per_round=1, decision_retries=1)
    plan = {(0, "CHEAP_TALK", 0): [{"type": "dm", "to": 2, "content": "move our prior meeting"}],
            (0, "DECISION", 0): [move(100, 0, 1), schedule()], (1, "DECISION", 0): [schedule()],
            (2, "VOLUNTARY", 0): [move(100, 2 if retry else 0, 1)],
            (2, "VOLUNTARY", 1): [move(100, 0, 1)]}
    local, trace, harness = run_planned(remote_harness, monkeypatch, task, plan, seat=2, cfg=cfg)
    remote_effects = [event for event in trace.events if event.data.get("phase") != "DECISION_RETRY"]
    assert normalized_events(trace.model_copy(update={"events": remote_effects})) == normalized_events(local)
    assert trace.final_state == local.final_state and trace.metrics == local.metrics
    assert trace.final_state["calendars"][2] == [None, {"meeting_id": 100, "cost": 2}, None]
    first = next(invocation for invocation in harness.invocations if invocation.kind == "turn")
    assert "move our prior meeting" in first.prompt and "ROUND 0 START" in first.prompt
    if retry:
        invocation = next(invocation for invocation in harness.invocations if invocation.phase == "DECISION_RETRY")
        assert invocation.parent_phase == "VOLUNTARY" and "do not use schedule" in invocation.prompt


def test_voluntary_retry_cannot_stage_schedule_and_uses_parent_topology(remote_harness):
    harness = remote_harness(seats=(2,))
    policy = harness.runtimes[2].policy.calls

    def calls(invocation):
        if invocation.kind != "turn":
            return policy(invocation)
        if invocation.phase == "VOLUNTARY":
            return [("env", "reschedule", {"item_id": 1, "from_slot": 2, "to_slot": 1, "justification": "retry"})]
        if invocation.phase == "DECISION_RETRY":
            return [("env", "schedule", {"meeting_id": 0, "slot": 0}),
                    ("comm", "send", {"channel": "dm", "to": 0, "content": "retry message"}),
                    ("env", "reschedule", {"item_id": 1, "from_slot": 0, "to_slot": 1, "justification": "move"})]
        return []

    harness.runtimes[2].policy.calls = calls
    cfg = config(num_agents=3, agents=[{"type": "scripted"}, {"type": "scripted"}, {"type": "scripted", "runtime": "external"}],
                 max_turns_per_round=1, decision_retries=1,
                 communication={"topology": {"default": {"graph": "complete", "channels": {"all_groupchat": {"enabled": True}}},
                                              "phases": {"VOLUNTARY": {"channels": {"dm": {"enabled": True}}}}}})
    task = scenario()
    task["calendars"].append([{"errand_id": 1, "cost": 2}, None, None])
    trace = CalendarGame(cfg, runtime_context=harness.context).run_with_scenario(task)
    invalid = [event for event in trace.events if event.type == "invalid_tool_call" and event.data["agent_id"] == 2]
    assert len(invalid) == 1 and invalid[0].data["tool_call"]["code"] == "wrong_phase"
    assert any(event.type == "dm_sent" and event.data["content"] == "retry message" and event.data["phase"] == "VOLUNTARY" for event in trace.events)
    assert trace.final_state["calendars"][2] == [None, {"errand_id": 1, "cost": 2}, None]


def test_remote_inbox_read_is_not_delivery_and_unread_messages_clear_at_round_boundary(remote_harness):
    harness = remote_harness(seats=(1,))
    original = harness.runtimes[1].policy.calls

    def calls(invocation):
        result = original(invocation)
        if invocation.phase == "DECISION" and invocation.observation["round"] == 0:
            result += [("comm", "send", {"channel": "dm", "to": 0, "content": "unread last decision"})]
        if invocation.kind == "turn":
            result = [("comm", "read_inbox", {}), *result]
        return result

    harness.runtimes[1].policy.calls = calls
    task = scenario()
    task["meetings"].append({"id": 1, "participants": [0, 1], "speaker_order": [0, 1], "duration": 1, "cost": 1})
    cfg = config(num_meetings=2, agents=[{"type": "scripted"}, {"type": "scripted", "runtime": "external"}],
                 communication={"topology": {"default": {"graph": "complete", "channels": {"dm": {"enabled": True}}},
                                              "phases": {"DECISION": {"channels": {"dm": {"enabled": True}}}}}})
    trace = CalendarGame(cfg, runtime_context=harness.context).run_with_scenario(task)
    cleared = [event for event in trace.events if event.type == "inbox_cleared"]
    assert len(cleared) == 1 and cleared[0].data["round"] == 1
    assert cleared[0].data["messages"][0]["content"] == "unread last decision"
    assert all("unread last decision" not in event.data["prompt_sent"] for event in trace.events
               if event.type == "turn_start" and event.data["round"] == 1)
    assert trace.metrics["meetings_scheduled"] == 2


@pytest.mark.parametrize("invalid", [False, True])
def test_reflection_is_measurement_only_seat_scoped_and_sanitized(remote_harness, invalid):
    harness = remote_harness(seats=(0, 1))
    memories = {0: [], 1: []}
    before = {}
    for seat, runtime in harness.runtimes.items():
        original = runtime.policy.calls

        def calls(invocation, original=original, seat=seat):
            if invocation.kind == "turn":
                memories[seat].append(invocation.prompt)
            return original(invocation)

        def reflect(invocation, seat=seat):
            harness.invocations.append(invocation)
            before[seat] = list(memories[seat])
            return {"text": harness.episode.seats[seat].secret,
                    "estimates": [{"agent_id": 99, "target_agent_id": 99, "slot": 99,
                                   "probability_free": 0.75, "probability_busy": 2 if invalid else 0.25,
                                   "belief_delta_occupied": -1, "estimate_state": 0, "confidence": 0.5,
                                   "private_text": harness.episode.seats[seat].secret,
                                   "logprobs": {"0": -0.1, "1": -2}}
                                  for _ in range(invocation.observation["num_slots"])]}

        runtime.policy.calls = calls
        runtime.policy.reflect = reflect
    secrets = [seat.secret for seat in harness.episode.seats.values()]
    cfg = config(enable_reflection=True, agents=[{"type": "scripted", "runtime": "external"}] * 2)
    trace = CalendarGame(cfg, runtime_context=harness.context).run_with_scenario(scenario())
    ends = [event for event in trace.events if event.type == "reflection_end"]
    assert len(ends) == 2 and memories == before
    for event in ends:
        seat = event.data["agent_id"]
        assert all(estimate["agent_id"] == seat and estimate["target_agent_id"] == 1 - seat
                   and estimate["slot"] == index for index, estimate in enumerate(event.data["estimates"]))
        assert event.data["raw_api_response"] is None and event.data["text"] is None
        assert event.data["estimates"][0]["probability_busy"] == (None if invalid else 0.25)
    reflections = [invocation for invocation in harness.invocations if invocation.kind == "reflect"]
    assert len(reflections) == 2 and all(invocation.capability is None for invocation in reflections)
    assert all(set(invocation.observation) == {"target_agent_id", "num_slots", "round"} for invocation in reflections)
    assert all(secret not in trace.model_dump_json() for secret in secrets)
