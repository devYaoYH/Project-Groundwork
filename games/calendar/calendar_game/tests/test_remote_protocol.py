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


LIFECYCLE_FIELDS = {"runtime", "protocol_version", "agent_info", "turn_id", "deadline", "closed_by", "attempts", "telemetry_source"}


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
def test_phase3_features_fail_explicitly(remote_harness, overrides):
    harness = remote_harness()
    with pytest.raises(ValueError, match="Phase 3"):
        CalendarGame(config(agents=[{"type": "scripted", "runtime": "external"}, {"type": "scripted"}], **overrides),
                     runtime_context=harness.context)


@pytest.mark.parametrize("runtime", ["local_process", "human", "unknown"])
def test_unimplemented_runtimes_fail_before_build(runtime):
    with pytest.raises(ValueError, match="not supported in Phase 2"):
        CalendarGame(config(agents=[{"type": "scripted", "runtime": runtime}, {"type": "scripted"}]))


def test_remote_configuration_requires_private_context():
    with pytest.raises(ValueError, match="privately provisioned"):
        CalendarGame(config(agents=[{"type": "scripted", "runtime": "external"}, {"type": "scripted"}]))


def test_remote_nonparticipant_guard_and_teardown(remote_harness):
    harness = remote_harness()
    game = CalendarGame(config(agents=[{"type": "scripted", "runtime": "external"}, {"type": "scripted"}]),
                        runtime_context=harness.context)
    task = scenario()
    task["meetings"][0]["participants"] = [1]
    task["meetings"][0]["speaker_order"] = [1]
    with pytest.raises(ValueError, match="non-participant/voluntary"):
        game.run_with_scenario(task)
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
    assert all(event.data["tool_call"]["code"] == "unsupported" for event in rejected)
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
                    ("env", "reschedule", {"item_id": 10, "from_slot": 0, "to_slot": 1, "justification": "unsupported"}),
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
