"""Real /2 staging, repeated-session parity, failure evidence and cleanup."""

from contextlib import ExitStack
import pytest
from a2a_agent.runtime_v2 import AgentRuntimeV2
from a2a_engine.remote.dispatch import LoopbackServer
from a2a_engine.remote.server import EnvironmentApp
from calendar_game.remote import CalendarRuntimeContext
from hidden_profile.environment import HiddenProfileEnvironment
from hidden_profile.remote import PROFILE_TOOLS
from hidden_profile.runtime_strategy import HiddenProfileStrategy


def run_remote(*, fail=False, names=None):
    invocations = []
    with ExitStack() as stack:
        env = EnvironmentApp(PROFILE_TOOLS)
        io = stack.enter_context(LoopbackServer(env.app))
        episode = env.registry.provision(
            "profiles", [0, 1, 2, 3], environment_id="hidden_profile"
        )
        runtime = AgentRuntimeV2()
        callback = stack.enter_context(LoopbackServer(runtime.app))
        policies = []
        for seat in range(4):
            policy = HiddenProfileStrategy()
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
                    io.base_url + "/episodes/profiles/join",
                    callback.base_url,
                    episode.seats[seat].ticket,
                    policy=policy,
                )
            )
        game = HiddenProfileEnvironment(
            {
                "discussion_cycles": 1,
                "agents": [
                    {
                        "type": "scripted",
                        "runtime": "external",
                        **({"id": names[i]} if names else {}),
                    }
                    for i in range(4)
                ],
            },
            dry_run=True,
            runtime_context=CalendarRuntimeContext(env, io, episode),
        )
        trace = game.run()
        return trace, invocations, policies, episode


def test_remote_repeated_session_parity_and_private_registration():
    expected = HiddenProfileEnvironment({"discussion_cycles": 1}, dry_run=True).run()
    actual, invocations, policies, episode = run_remote()
    assert actual.final_state == expected.final_state
    assert actual.metrics == expected.metrics
    assert [(e.type, e.data) for e in actual.events] == [
        (e.type, e.data) for e in expected.events
    ]
    registrations = [i for i in invocations if i.kind == "register"]
    assert len(registrations) == 4
    assert all(
        set(i.observation) == {"role", "participant", "participant_id"}
        for i in registrations
    )
    assert all(p.agent.request_history_lengths[-1] > 10 for p in policies)
    assert not episode.active


def test_remote_failure_never_invents_consensus():
    with pytest.raises(
        RuntimeError, match="hidden_profile runtime action failed"
    ) as error:
        run_remote(fail=True)
    assert "private" not in str(error.value)


def test_action_staging_dedup_does_not_commit_twice():
    from datetime import datetime, timedelta, timezone
    from a2a_engine.remote.contract import CapabilityClaims, TurnInvocationV2
    from a2a_engine.remote.capabilities import Capabilities
    from a2a_engine.remote.turns import TurnRecorder
    from hidden_profile.remote import stage_text

    invocation = TurnInvocationV2(
        environment_id="hidden_profile",
        episode_id="e",
        attempt_id="a",
        seat=0,
        turn_id="t",
        kind="turn",
        operation="act",
        phase="ACT",
        deadline=datetime.now(timezone.utc) + timedelta(seconds=10),
        mcp={
            name: f"http://127.0.0.1:1234/{name}"
            for name in ("env", "comm", "lifecycle")
        },
    )
    claims = CapabilityClaims(
        episode_id="e",
        attempt_id="a",
        seat=0,
        turn_id="t",
        phase="ACT",
        exp=invocation.deadline.timestamp(),
        allowed_tools=["env.submit_action"],
    )
    recorder = TurnRecorder(invocation, claims, Capabilities(), stage_text)
    tool = PROFILE_TOOLS[0]
    assert (
        recorder.call(tool, {"kind": "propose", "candidate": "B"}, "id").status
        == "accepted"
    )
    assert (
        recorder.call(tool, {"kind": "propose", "candidate": "B"}, "id").status
        == "accepted"
    )
    assert (
        recorder.call(tool, {"kind": "endorse", "candidate": "B"}, "id").code
        == "call_id_conflict"
    )
    assert (
        recorder.call(tool, {"kind": "endorse", "candidate": "B"}, "second").code
        == "already_submitted"
    )
    assert len([r for r in recorder.close() if "action" in r]) == 1


def test_cancelled_provider_cannot_append_private_history_or_overlap():
    import asyncio
    import threading
    from types import SimpleNamespace

    entered, release = threading.Event(), threading.Event()

    class BlockingProvider:
        def oneshot(self, messages):
            entered.set()
            assert release.wait(5)
            return '{"kind":"pass"}'

    policy = HiddenProfileStrategy(client=BlockingProvider())

    async def play():
        await policy.calls(
            SimpleNamespace(
                kind="register", observation={"role": "participant", "participant": 1}
            )
        )
        invocation = SimpleNamespace(kind="turn", observation={"round": 1})
        task = asyncio.create_task(policy.calls(invocation))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert policy.model_lock.locked()
            with pytest.raises(RuntimeError, match="still active"):
                await policy.calls(invocation)
        finally:
            release.set()

    asyncio.run(play())
    assert policy.agent.history == []
    assert not policy.model_lock.locked()


def test_remote_custom_identity_is_same_on_agent_and_environment():
    names = ["alice", "bob", "charlie", "dana"]
    trace, invocations, policies, _ = run_remote(names=names)
    assert [policy.agent.name for policy in policies] == names
    assert {
        event.data["speaker"]
        for event in trace.events
        if event.type in ("message", "observation", "sealed_report")
    } == set(names)
    registrations = sorted(
        (i for i in invocations if i.kind == "register"), key=lambda i: i.seat
    )
    assert [i.observation["participant_id"] for i in registrations] == names
