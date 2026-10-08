import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_engine.remote.capabilities import Capabilities
from a2a_engine.remote.contract import CapabilityClaims, ToolOutcome, TurnCompletion, TurnInvocation
from a2a_engine.remote.dispatch import LoopbackServer, TurnDispatcher
from a2a_engine.remote.server import EnvironmentApp
from a2a_engine.remote.turns import ToolSpec, TurnRecorder


class Input(BaseModel):
    value: int


SPEC = ToolSpec("put", "env", Input, frozenset({"ACT"}), "end_of_turn")


def recorder(**kwargs):
    invocation = TurnInvocation(episode_id="test", seat=0, turn_id="turn", kind="turn", phase="ACT",
                                deadline=datetime.now(timezone.utc) + timedelta(seconds=10))
    claims = CapabilityClaims(episode_id="test", attempt_id="attempt", seat=0, turn_id="turn",
                              phase="ACT", allowed_tools=["env.put"], exp=invocation.deadline.timestamp())
    capabilities = Capabilities()
    invocation.capability = capabilities.mint(claims)

    def stage(spec, parsed, turn):
        return ToolOutcome(status="accepted", resolves=spec.resolves), {"value": parsed.value}

    return TurnRecorder(invocation, claims, capabilities, stage, **kwargs)


@pytest.mark.parametrize("reason", ["completed", "condition", "deadline", "unreachable"])
def test_first_close_wins_and_snapshot_is_immutable(reason):
    turn = recorder()
    turn.call(SPEC, {"value": 1}, "call")
    snapshot = turn.close(reason)
    snapshot[0]["action"]["value"] = 9
    assert turn.close("deadline") == [{"action": {"value": 1}}]
    assert turn.closed_by == reason
    assert turn.call(SPEC, {"value": 2}, "late").code == "closed_turn"
    with pytest.raises(ValueError):
        turn.capabilities.verify(turn.invocation.capability, episode_id="test", attempt_id="attempt")


def test_atomic_payload_sensitive_dedup_and_reservation():
    turn = recorder()
    original = turn.handler

    def reserve(*args):
        args[-1].reserved += 1
        return original(*args)

    turn.handler = reserve
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: turn.call(SPEC, {"value": 1}, "same"), range(40)))
    assert all(outcome.status == "accepted" for outcome in results)
    assert turn.reserved == 1 and turn.calls == 1 and len(turn.records) == 1
    assert turn.call(SPEC, {"value": 2}, "same").code == "call_id_conflict"
    assert turn.reserved == 1


def test_completion_deadline_race_uses_monotonic_clock():
    now = [10.0]
    turn = recorder(clock=lambda: now[0], deadline=11)
    turn.call(SPEC, {"value": 1}, "staged")
    now[0] = 11
    turn.complete(TurnCompletion(turn_id="turn"))
    assert turn.closed_by == "deadline" and turn.completion is None
    assert turn.close() == [{"action": {"value": 1}}]


def test_condition_closes_and_calendar_style_yield_keeps_whole_cell():
    turn = recorder(condition=lambda turn: len(turn.records) == 2)
    turn.call(SPEC, {"value": 1}, "first")
    assert not turn.closed
    turn.call(SPEC, {"value": 2}, "second")
    assert turn.closed_by == "condition" and len(turn.close()) == 2
    turn = recorder(honor_completion=False)
    turn.complete(TurnCompletion(turn_id="turn"))
    assert not turn.closed
    turn.call(SPEC, {"value": 3}, "after_yield")
    assert len(turn.close("deadline")) == 1


def test_bounded_calls_and_bytes_include_invalid_calls():
    turn = recorder(max_calls=3)
    for index in range(3):
        assert turn.call(SPEC, {"value": "bad"}, str(index)).code == "invalid_arguments"
    assert turn.call(SPEC, {"value": 1}, "four").code == "call_cap"
    assert len(turn.cache) == 3 and len(turn.records) == 4
    turn = recorder(max_bytes=10, max_calls=2)
    assert turn.call(SPEC, {"value": 1}, "one").code == "body_cap"
    assert turn.call(SPEC, {"value": 1}, "two").code == "body_cap"
    assert len(turn.records) <= 2 and len(turn.cache) == 2


def test_concurrent_completion_close_keeps_one_outcome():
    turn = recorder()
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(turn.close, ["deadline", "unreachable", "completed", "condition"] * 10))
    first = turn.closed_by
    turn.complete(TurnCompletion(turn_id="turn"))
    assert turn.closed_by == first and turn.completion is None


def test_dispatch_closes_at_io_completion_before_worker_poll(monkeypatch):
    from types import SimpleNamespace
    from a2a_engine.remote import dispatch

    turn = recorder()
    timeouts = []

    async def response(url, invocation, key, *, timeout):
        timeouts.append(timeout)
        return TurnCompletion(turn_id=invocation.turn_id).model_dump_json().encode()

    monkeypatch.setattr(dispatch, "post_signed", response)
    seat = SimpleNamespace(callback_url="http://127.0.0.1:123", secret="private")
    completion = asyncio.run(TurnDispatcher(None)._deliver(seat, turn.invocation, turn.deadline, turn))
    assert turn.closed_by == "completed" and turn.completion == completion
    assert timeouts[0] > 9  # The default request timeout is the remaining turn budget.
    assert turn.call(SPEC, {"value": 1}, "after_completion").code == "closed_turn"


def test_immediate_spec_requires_handler():
    with pytest.raises(ValueError, match="worker handler"):
        ToolSpec("increment", "env", Input, frozenset({"ACT"}), "immediate")


def test_immediate_handler_returns_real_result_on_blocked_worker():
    from a2a_agent.mcp_client import connect

    worker = threading.get_ident()
    context = ContextVar("worker_context", default=None)
    context.set("event_sink")
    executions = []

    def immediate(parsed, turn):
        executions.append((threading.get_ident(), context.get()))
        return ToolOutcome(status="ok", resolves="immediate", data={"answer": parsed.value + 1})

    spec = ToolSpec("increment", "env", Input, frozenset({"ACT"}), "immediate", handler=immediate)
    environment = EnvironmentApp((spec,))
    with LoopbackServer(environment.app) as io:
        episode = environment.registry.provision("synthetic", (0,))
        seat = episode.seats[0]
        seat.secret = "private"
        seat.registered = True
        seat.ready.set()
        invocation = TurnInvocation(episode_id="synthetic", seat=0, turn_id="immediate", kind="turn", phase="ACT",
                                    deadline=datetime.now(timezone.utc) + timedelta(seconds=3),
                                    mcp={"env": f"{io.base_url}/episodes/synthetic/env/mcp"})
        claims = CapabilityClaims(episode_id="synthetic", attempt_id=episode.attempt_id, seat=0, turn_id="immediate",
                                  phase="ACT", allowed_tools=["env.increment"], exp=invocation.deadline.timestamp())
        invocation.capability = environment.registry.capabilities.mint(claims)
        seat.recorder = TurnRecorder(invocation, claims, environment.registry.capabilities, None)

        async def callback(request):
            async with connect(invocation.mcp["env"], invocation.capability) as session:
                results = await asyncio.gather(*[session.call_tool("increment", {"value": 41},
                                                                   meta={"a2a/call_id": "same"}) for _ in range(4)])
                assert all(result.structuredContent["data"] == {"answer": 42} for result in results)
            return JSONResponse(TurnCompletion(turn_id=invocation.turn_id).model_dump(mode="json"))

        with LoopbackServer(Starlette(routes=[Route("/turns", callback, methods=["POST"])])) as external:
            seat.callback_url = external.base_url
            completion = TurnDispatcher(io).dispatch(seat, invocation, seat.recorder)
            assert completion.turn_id == "immediate"
            assert executions == [(worker, "event_sink")]
            assert seat.recorder.closed_by == "completed"
        environment.registry.unregister(episode)
