import time
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from a2a_engine.remote.capabilities import AuthorizationError, Capabilities
from a2a_engine.remote.contract import CapabilityClaims, ToolOutcome, TurnFinished, TurnInvocationV2, WireModel
from a2a_engine.remote.lifecycle import LifecycleRegistry
from a2a_engine.remote.network import NetworkPolicy
from a2a_engine.remote.turns import TurnRecorder, ToolSpec


def invocation(**updates):
    return TurnInvocationV2(environment_id="calendar", episode_id="episode", attempt_id="attempt", seat=0,
                            turn_id="turn", kind="turn", operation="act", phase="DECISION",
                            deadline=datetime.now(timezone.utc) + timedelta(seconds=10), mcp={}, **updates)


def terminal(value, **updates):
    return TurnFinished(**value.identity().model_dump(), status="succeeded", **updates)


def test_completion_identity_dedup_conflict_and_grace():
    clock = [0.0]
    registry = LifecycleRegistry(clock=lambda: clock[0], grace_s=2, capacity=1)
    value = invocation()
    record = registry.open(value, deadline=10)
    for field, replacement in [("attempt_id", "other"), ("seat", 1), ("environment_id", "other")]:
        with pytest.raises(AuthorizationError):
            registry.finish(record.credential, terminal(value).model_copy(update={field: replacement}))
    with pytest.raises(AuthorizationError):
        registry.finish("wrong", terminal(value))
    with pytest.raises(ValueError, match="capacity"):
        registry.open(invocation().model_copy(update={"turn_id": "next"}), deadline=10)
    assert registry.finish(record.credential, terminal(value)).disposition == "recorded"
    assert record.event.is_set()
    assert registry.finish(record.credential, terminal(value)).disposition == "duplicate"
    with pytest.raises(ValueError, match="conflicting"):
        registry.finish(record.credential, terminal(value, telemetry={"latency_ms": 2}))
    clock[0] = 3
    with pytest.raises(AuthorizationError):
        registry.finish(record.credential, terminal(value))
    assert not registry.records


def test_deadline_closes_recorder_and_lifecycle_is_not_action_authority():
    value = invocation()
    capabilities = Capabilities()
    claims = CapabilityClaims(episode_id=value.episode_id, attempt_id=value.attempt_id, seat=0,
                              turn_id=value.turn_id, phase=value.phase, allowed_tools=[], exp=time.time() + 10)
    value.capability = capabilities.mint(claims)
    recorder = TurnRecorder(value, claims, capabilities, lambda *args: None, max_calls=0)
    registry = LifecycleRegistry()
    record = registry.open(value, deadline=time.monotonic() - 1, recorder=recorder)
    assert registry.finish(record.credential, terminal(value)).disposition == "closed"
    assert recorder.closed_by == "deadline" and recorder.calls == 0
    with pytest.raises(AuthorizationError):
        capabilities.verify(value.capability, episode_id=value.episode_id, attempt_id=value.attempt_id)
    with pytest.raises(AuthorizationError):
        capabilities.verify(record.credential, episode_id=value.episode_id, attempt_id=value.attempt_id)


def test_lifecycle_success_does_not_spend_exhausted_call_budget():
    value = invocation()
    capabilities = Capabilities()
    claims = CapabilityClaims(episode_id=value.episode_id, attempt_id=value.attempt_id, seat=0,
                              turn_id=value.turn_id, phase=value.phase, allowed_tools=[], exp=time.time() + 10)
    capabilities.mint(claims)
    recorder = TurnRecorder(value, claims, capabilities, lambda *args: None, max_calls=0)
    registry = LifecycleRegistry()
    record = registry.open(value, deadline=time.monotonic() + 10, recorder=recorder)
    assert registry.finish(record.credential, terminal(value)).disposition == "recorded"
    assert recorder.closed_by == "completed" and recorder.calls == 0
    registry.close_episode(value.episode_id, value.attempt_id)


@pytest.mark.parametrize("url", ["http://other:8000/mcp", "http://10.0.0.1:8000/mcp", "https://agent:8000/mcp",
                                "http://agent:8000/mcp?x=1", "http://user@agent:8000/mcp", "http://agent:8000/a/../mcp"])
def test_network_rejects_unprovisioned_endpoints(url):
    policy = NetworkPolicy(advertised_origin="http://environment:8000", allowed_origins=["http://agent:8000"])
    with pytest.raises(ValueError):
        policy.validate(url)


def test_explicit_network_and_legacy_loopback_are_separate():
    policy = NetworkPolicy(advertised_origin="http://environment:8000", allowed_origins=["http://agent:8000"])
    assert policy.validate("http://agent:8000/mcp") == "http://agent:8000/mcp"
    with pytest.raises(ValueError):
        NetworkPolicy().validate("http://agent:8000/mcp")


def test_completion_serializes_with_tool_admission_and_snapshot_is_immutable():
    value = invocation()
    capabilities = Capabilities()
    claims = CapabilityClaims(episode_id=value.episode_id, attempt_id=value.attempt_id, seat=0,
                              turn_id=value.turn_id, phase=value.phase, allowed_tools=["env.write"], exp=time.time() + 10)
    capabilities.mint(claims)
    entered = threading.Event()
    release = threading.Event()

    class Input(WireModel):
        pass

    def handler(spec, parsed, recorder):
        entered.set()
        assert release.wait(2)
        return ToolOutcome(status="accepted", resolves="end_of_turn"), {"value": "prior"}

    recorder = TurnRecorder(value, claims, capabilities, handler)
    registry = LifecycleRegistry()
    record = registry.open(value, deadline=time.monotonic() + 10, recorder=recorder)
    spec = ToolSpec("write", "env", Input, frozenset({"DECISION"}), "end_of_turn")
    with ThreadPoolExecutor(max_workers=2) as pool:
        action = pool.submit(recorder.call, spec, {}, "first")
        assert entered.wait(1)
        finished = pool.submit(registry.finish, record.credential, terminal(value))
        assert not finished.done()
        release.set()
        assert action.result(2).status == "accepted"
        assert finished.result(2).disposition == "recorded"
    snapshot = recorder.close()
    assert snapshot == [{"action": {"value": "prior"}}]
    assert recorder.call(spec, {}, "late").code == "closed_turn"
    assert recorder.close() == snapshot


def test_lazy_expiry_cannot_restart_post_deadline_credential_grace():
    clock = [0.0]
    registry = LifecycleRegistry(clock=lambda: clock[0], grace_s=2)
    value = invocation()
    record = registry.open(value, deadline=1)
    clock[0] = 100
    with pytest.raises(AuthorizationError):
        registry.finish(record.credential, terminal(value))
    assert record.closed_by == "deadline" and record.event.is_set() and not registry.records


def test_probe_builds_the_pinned_local_graph_api_without_execution():
    from a2a_engine.remote.probe import build_probe
    graph = build_probe().get_graph()
    assert set(graph.nodes) == {"__start__", "initialize", "agent_turn", "finalize", "__end__"}
    assert {(edge.source, edge.target) for edge in graph.edges} == {
        ("__start__", "initialize"), ("agent_turn", "finalize"), ("initialize", "agent_turn"), ("finalize", "__end__")}
