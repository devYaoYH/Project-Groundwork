import asyncio
import copy

import pytest

from calendar_game.game import CalendarGame
from calendar_game.tests._legacy_workflow_oracle import LegacyCalendarGame
from calendar_game.tests.test_remote_parity import normalized_events


@pytest.mark.parametrize("protocol", [1, 2])
def test_remote_integrated_retries_and_reflection_match_pre_refactor(remote_harness, remote_harness_v2, protocol):
    factory = remote_harness if protocol == 1 else remote_harness_v2
    cfg = {"seed": 42, "num_agents": 3, "num_slots": 3, "num_meetings": 1,
           "agents": [{"type": "scripted", "runtime": "external"}] * 3,
           "enable_fallback": False, "enable_reflection": True, "reflection_max_parallelism": 3,
           "max_turns_per_round": 1, "decision_retries": 1, "turn_timeout_s": 10}
    scenario = {"calendars": [[None] * 3 for _ in range(3)], "meetings": [
        {"id": 0, "participants": [0, 1, 2], "speaker_order": [2, 0, 1], "cost": 1, "duration": 1}]}
    traces, observations = [], []
    for cls in (LegacyCalendarGame, CalendarGame):
        harness = factory(seats=(0, 1, 2))
        policies = harness.policies if protocol == 2 else {seat: r.policy for seat, r in harness.runtimes.items()}
        calls = []
        active, peaks = {}, {}
        for seat, policy in policies.items():
            original = policy.calls

            def decide(invocation, original=original):
                calls.append((invocation.kind, invocation.seat, invocation.phase, invocation.parent_phase,
                              copy.deepcopy(invocation.observation), invocation.prompt, copy.deepcopy(invocation.inbox)))
                if invocation.phase == "DECISION":
                    return [("env", "schedule", {"meeting_id": 0, "slot": 999})]
                if invocation.phase == "DECISION_RETRY":
                    return [("env", "schedule", {"meeting_id": 0, "slot": 0})]
                return original(invocation)

            async def reflect(invocation):
                seat = invocation.seat
                active[seat] = active.get(seat, 0) + 1
                peaks[seat] = max(peaks.get(seat, 0), active[seat])
                await asyncio.sleep(0.04 if invocation.observation["target_agent_id"] == 1 else 0.01)
                active[seat] -= 1
                return {"estimates": [{"slot": slot, "estimate_state": 0, "probability_free": 1,
                    "probability_busy": 0, "confidence": 1, "logprobs": {}} for slot in range(3)]}

            policy.calls, policy.reflect = decide, reflect
        trace = cls(copy.deepcopy(cfg), runtime_context=harness.context).run_with_scenario(copy.deepcopy(scenario))
        assert trace.metrics["meetings_scheduled"] == 1
        assert trace.metrics["reflection_estimate_count"] == 18
        assert peaks == {0: 1, 1: 1, 2: 1}
        if protocol == 2:
            records = harness.environment.registry.lifecycle.records.values()
            assert all(record.acceptance.status == "accepted" and record.finished.status == "succeeded" for record in records)
        traces.append(trace)
        observations.append(calls)
    assert observations[0] == observations[1]
    assert normalized_events(traces[0]) == normalized_events(traces[1])
    assert traces[0].final_state == traces[1].final_state
    assert traces[0].metrics == traces[1].metrics


@pytest.mark.parametrize("outcome", ["success", "depth_exceeded", "no_feasible_slot", "dry_run"])
def test_complete_fallback_routes_match_local_and_both_transports(remote_harness, remote_harness_v2, outcome, monkeypatch):
    from calendar_game.fallback import FallbackDepthExceeded, FallbackImpossible
    from calendar_game.tests.test_workflow import play, setup_case
    cfg, scenario = setup_case("empty", cap=0, retries=0)
    cfg.update(num_agents=2, agents=[{"type": "scripted"}] * 2, enable_fallback=True,
               enable_reflection=False, turn_timeout_s=10)
    scenario["calendars"] = scenario["calendars"][:2]
    if outcome in {"depth_exceeded", "no_feasible_slot"}:
        def search(agents, meeting, *args, **kwargs):
            if outcome == "depth_exceeded":
                raise FallbackDepthExceeded(meeting["id"], 2)
            raise FallbackImpossible(meeting["id"])
        for module in ("calendar_game.game", "calendar_game.tests._legacy_workflow_oracle"):
            monkeypatch.setattr(module + ".find_fallback_slot", search)
    if outcome == "dry_run":
        expected = LegacyCalendarGame(copy.deepcopy(cfg), dry_run=True).run_with_scenario(copy.deepcopy(scenario))
    else:
        expected, _ = play(LegacyCalendarGame, "empty", cfg, scenario)
    for protocol, factory in ((1, remote_harness), (2, remote_harness_v2)):
        harness = factory(seats=(0, 1))
        policies = harness.policies if protocol == 2 else {seat: runtime.policy for seat, runtime in harness.runtimes.items()}
        if outcome != "dry_run":
            for policy in policies.values():
                original = policy.calls
                def calls(invocation, original=original):
                    return [] if invocation.kind == "turn" else original(invocation)
                policy.calls = calls
        remote_cfg = {**cfg, "agents": [{"type": "scripted", "runtime": "external"}] * 2}
        actual = CalendarGame(remote_cfg, dry_run=outcome == "dry_run", runtime_context=harness.context).run_with_scenario(copy.deepcopy(scenario))
        assert normalized_events(actual) == normalized_events(expected)
        assert actual.final_state == expected.final_state
        assert actual.metrics == expected.metrics
