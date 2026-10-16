"""Real /2 staging, local/remote parity, failure evidence and cleanup."""

from contextlib import ExitStack
import pytest
from a2a_agent.runtime_v2 import AgentRuntimeV2
from a2a_engine.remote.dispatch import LoopbackServer
from a2a_engine.remote.server import EnvironmentApp
from calendar_game.remote import CalendarRuntimeContext
from hurricane.environment import HurricaneEnvironment
from hurricane.remote import HURRICANE_TOOLS
from hurricane.runtime_strategy import HurricaneStrategy, submission

CONFIG = {"num_rounds": 3, "seed": 7, "history_window": 2}


def run_remote(*, fail=False, names=None):
    invocations = []
    with ExitStack() as stack:
        env = EnvironmentApp(HURRICANE_TOOLS)
        io = stack.enter_context(LoopbackServer(env.app))
        episode = env.registry.provision("storms", [0, 1, 2], environment_id="hurricane")
        runtime = AgentRuntimeV2()
        callback = stack.enter_context(LoopbackServer(runtime.app))
        policies = []
        for seat in range(3):
            policy = HurricaneStrategy()
            policies.append(policy)
            original = policy.calls

            async def calls(invocation, original=original):
                invocations.append(invocation.model_copy(deep=True))
                if fail and invocation.kind == "turn":
                    raise RuntimeError("private provider details")
                return await original(invocation)

            policy.calls = calls
            callback.run(
                runtime.join(
                    io.base_url + "/episodes/storms/join",
                    callback.base_url,
                    episode.seats[seat].ticket,
                    policy=policy,
                )
            )
        game = HurricaneEnvironment(
            {
                **CONFIG,
                "agents": [
                    {"type": "scripted", "runtime": "external", **({"id": names[i]} if names else {})}
                    for i in range(3)
                ],
            },
            dry_run=True,
            runtime_context=CalendarRuntimeContext(env, io, episode),
        )
        trace = game.run()
        return trace, invocations, policies, episode


def test_remote_session_parity_and_private_registration():
    expected = HurricaneEnvironment(CONFIG, dry_run=True).run()
    actual, invocations, policies, episode = run_remote()
    assert actual.final_state == expected.final_state
    assert actual.metrics == expected.metrics
    assert [(e.type, e.data) for e in actual.events] == [(e.type, e.data) for e in expected.events]
    registrations = [i for i in invocations if i.kind == "register"]
    assert len(registrations) == 3
    assert all(set(i.observation) == {"role", "participant", "participant_id", "history_window"} for i in registrations)
    turns = [i for i in invocations if i.kind == "turn"]
    assert len(turns) == 3 * 3  # scripted seats never convene or message privately
    assert all("landfall_zone" not in i.observation for i in turns)
    assert all(p.agent.request_history_lengths[-1] == 4 for p in policies)
    assert not episode.active


def test_remote_failure_never_invents_an_action():
    with pytest.raises(RuntimeError, match="hurricane runtime action failed") as error:
        run_remote(fail=True)
    assert "private" not in str(error.value)


def test_submission_normalizes_turn_shapes():
    assert submission({"actions": [{"kind": "pass"}, 3]}) == {"actions": [{"kind": "pass"}]}
    assert submission({"kind": "leave"}) == {"actions": [{"kind": "leave"}]}
    assert submission({"parse_failed": True}) == {"parse_failed": True}
    assert submission({"actions": "x"}) == {"parse_failed": True}


def test_turn_staging_dedup_does_not_commit_twice():
    from datetime import datetime, timedelta, timezone
    from a2a_engine.remote.contract import CapabilityClaims, TurnInvocationV2
    from a2a_engine.remote.capabilities import Capabilities
    from a2a_engine.remote.turns import TurnRecorder
    from hurricane.remote import stage_turn

    invocation = TurnInvocationV2(
        environment_id="hurricane", episode_id="e", attempt_id="a", seat=0, turn_id="t",
        kind="turn", operation="act", phase="ACT",
        deadline=datetime.now(timezone.utc) + timedelta(seconds=10),
        mcp={name: f"http://127.0.0.1:1234/{name}" for name in ("env", "comm", "lifecycle")},
    )
    claims = CapabilityClaims(
        episode_id="e", attempt_id="a", seat=0, turn_id="t", phase="ACT",
        exp=invocation.deadline.timestamp(), allowed_tools=["env.submit_turn"],
    )
    recorder = TurnRecorder(invocation, claims, Capabilities(), stage_turn)
    tool = HURRICANE_TOOLS[0]
    turn = {"actions": [{"kind": "evacuate", "zones": [3]}, {"kind": "dm", "to": 2, "text": "hi", "share": True}]}
    assert recorder.call(tool, turn, "id").status == "accepted"
    assert recorder.call(tool, turn, "id").status == "accepted"
    assert recorder.call(tool, {"actions": [{"kind": "pass"}]}, "id").code == "call_id_conflict"
    assert recorder.call(tool, {"actions": [{"kind": "pass"}]}, "second").code == "already_submitted"
    records = [r for r in recorder.close() if "action" in r]
    assert records == [{"action": {"actions": [{"kind": "evacuate", "zones": [3]},
                                               {"kind": "dm", "to": 2, "text": "hi", "share": True}]}}]


def test_remote_custom_identity_is_shared_by_agent_and_environment():
    names = ["ana", "bo", "cy"]
    trace, invocations, policies, _ = run_remote(names=names)
    assert [policy.agent.name for policy in policies] == names
    assert {e.data["speaker"] for e in trace.events if e.type in ("message", "probe", "observation")} == set(names)
    registrations = sorted((i for i in invocations if i.kind == "register"), key=lambda i: i.seat)
    assert [i.observation["participant_id"] for i in registrations] == names
    assert all(i.observation["history_window"] == 2 for i in registrations)
