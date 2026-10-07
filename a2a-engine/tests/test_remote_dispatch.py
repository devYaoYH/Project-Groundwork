import asyncio
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import anyio
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_engine.llm.retry import RetryPolicy
from a2a_engine.remote.contract import TurnCompletion, TurnInvocation
from a2a_engine.remote.dispatch import DeliveryFailure, LoopbackServer, TurnDispatcher, deliver_with_retry, delivery_retryable


FAST = RetryPolicy(max_attempts=3, backoff_base=0, backoff_max=0, jitter_ratio=0)


def test_fake_clock_clips_backoff_and_stops_before_next_attempt():
    now = [0.0]
    sleeps, calls = [], []

    async def failing(remaining):
        calls.append(remaining)
        now[0] += 0.25
        raise ConnectionError("secret must not escape")

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    with pytest.raises(DeliveryFailure) as info:
        asyncio.run(deliver_with_retry(failing, deadline=1, clock=lambda: now[0], sleep=sleep,
                                      policy=RetryPolicy(backoff_base=100, jitter_ratio=0)))
    assert calls == [1] and sleeps == [0.75]
    assert info.value.closed_by == "deadline" and info.value.attempts == 1
    assert "secret" not in str(info.value)


@pytest.mark.parametrize("status,attempts", [(500, 3), (502, 3), (503, 3), (599, 3), (400, 1), (401, 1), (403, 1), (422, 1), (429, 1)])
def test_delivery_classification(status, attempts):
    calls = []

    async def fail(remaining):
        calls.append(remaining)
        response = httpx.Response(status, request=httpx.Request("POST", "http://127.0.0.1:123/turns"))
        response.raise_for_status()

    with pytest.raises(DeliveryFailure) as info:
        asyncio.run(deliver_with_retry(fail, deadline=time.monotonic() + 3, policy=FAST))
    assert len(calls) == attempts == info.value.attempts
    assert info.value.closed_by == "unreachable"


def test_hung_request_is_cancelled_at_absolute_deadline():
    cancelled = []

    async def hung(remaining):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.append(True)

    start = time.monotonic()
    with pytest.raises(DeliveryFailure) as info:
        asyncio.run(deliver_with_retry(hung, deadline=start + 0.05, policy=FAST))
    assert time.monotonic() - start < 1
    assert info.value.closed_by == "deadline" and cancelled == [True]


def test_hung_backoff_is_cancelled_at_absolute_deadline():
    cancelled = []

    async def fail(remaining):
        raise ConnectionError()

    async def sleep(delay):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.append(True)

    start = time.monotonic()
    with pytest.raises(DeliveryFailure) as info:
        asyncio.run(deliver_with_retry(fail, deadline=start + 0.05, policy=FAST, sleep=sleep))
    assert time.monotonic() - start < 1
    assert info.value.closed_by == "deadline" and cancelled == [True]


def test_result_after_injected_deadline_is_not_a_success():
    now = [0]

    async def late(remaining):
        now[0] = 2
        return "late"

    with pytest.raises(DeliveryFailure) as info:
        asyncio.run(deliver_with_retry(late, deadline=1, clock=lambda: now[0], policy=FAST))
    assert info.value.closed_by == "deadline" and info.value.attempts == 1


@pytest.mark.parametrize("error", [anyio.EndOfStream(), anyio.BrokenResourceError(), anyio.ClosedResourceError(),
                                   McpError(ErrorData(code=408, message="timeout"))])
def test_sdk_transport_errors_are_retryable_inside_exception_groups(error):
    assert delivery_retryable(ExceptionGroup("SDK", [error]))
    assert not delivery_retryable(ExceptionGroup("SDK", [error, ValueError("schema")]))
    assert not delivery_retryable(McpError(ErrorData(code=-32602, message="invalid arguments")))


@pytest.mark.parametrize("mode", ["5xx", "lost", "schema", "oversized", "hung"])
def test_real_http_retry_and_bounded_failures(mode):
    calls = []
    value = TurnInvocation(episode_id="test", seat=0, turn_id="push", kind="round_start",
                           deadline=datetime.now(timezone.utc) + timedelta(seconds=0.6))

    async def callback(request):
        calls.append(await request.body())
        if mode == "5xx" and len(calls) == 1:
            return JSONResponse({"code": "retry"}, status_code=503)
        if mode == "lost" and len(calls) == 1:
            await asyncio.sleep(0.2)
        if mode == "schema":
            return JSONResponse({"turn_id": "wrong"})
        if mode == "oversized":
            return JSONResponse({"padding": "x" * 300000})
        if mode == "hung":
            await asyncio.sleep(2)
        return JSONResponse(TurnCompletion(turn_id=value.turn_id).model_dump(mode="json"))

    with LoopbackServer(Starlette(routes=[Route("/turns", callback, methods=["POST"])])) as callback_io:
        seat = SimpleNamespace(callback_url=callback_io.base_url, secret="private")
        dispatcher = TurnDispatcher(callback_io, policy=FAST, request_timeout_s=0.1)
        start = time.monotonic()
        if mode in {"5xx", "lost"}:
            assert dispatcher.dispatch(seat, value).turn_id == "push"
            assert len(calls) == 2 and calls[0] == calls[1]
        else:
            with pytest.raises(DeliveryFailure):
                dispatcher.dispatch(seat, value)
            assert len(calls) == (3 if mode == "hung" else 1)
        assert time.monotonic() - start < 1
