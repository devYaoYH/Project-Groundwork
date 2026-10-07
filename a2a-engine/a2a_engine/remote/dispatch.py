"""Loopback I/O on a dedicated ASGI thread; no game mutations or events."""

import asyncio
import hashlib
import hmac
import ipaddress
import socket
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import anyio
import httpx
import uvicorn
from mcp.shared.exceptions import McpError
from starlette.requests import Request
from starlette.responses import JSONResponse

from a2a_engine.llm.retry import RetryPolicy, compute_delay
from .contract import MAX_BODY_BYTES, TurnCompletion

DELIVERY_POLICY = RetryPolicy(max_attempts=3, backoff_base=0.05, backoff_max=0.25)


def delivery_retryable(exc):
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(delivery_retryable(item) for item in exc.exceptions)
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    if isinstance(exc, McpError):
        return exc.error.code == httpx.codes.REQUEST_TIMEOUT
    return isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError,
                            anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError))


class DeliveryFailure(Exception):
    def __init__(self, closed_by, attempts):
        super().__init__(closed_by)
        self.closed_by = closed_by
        self.attempts = attempts


async def deliver_with_retry(fn, *, deadline, policy=DELIVERY_POLICY, clock=time.monotonic,
                             sleep=asyncio.sleep, on_attempt=None):
    """Bound requests and backoff by one monotonic deadline; never log credentials."""
    attempts = 0
    for index in range(policy.max_attempts):
        remaining = deadline - clock()
        if remaining <= 0:
            raise DeliveryFailure("deadline", attempts)
        attempts += 1
        if on_attempt:
            on_attempt(attempts)
        try:
            async with asyncio.timeout(remaining):
                result = await fn(remaining)
                if clock() >= deadline:
                    raise TimeoutError("delivery deadline exceeded")
                return result
        except Exception as exc:
            if clock() >= deadline:
                raise DeliveryFailure("deadline", attempts) from None
            if not delivery_retryable(exc) or index == policy.max_attempts - 1:
                raise DeliveryFailure("unreachable", attempts) from None
            remaining = max(0, deadline - clock())
            try:
                async with asyncio.timeout(remaining):
                    await sleep(min(compute_delay(index, exc, policy), remaining))
            except TimeoutError:
                raise DeliveryFailure("deadline", attempts) from None
    raise DeliveryFailure("unreachable", attempts)


def loopback_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "http" or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or not ipaddress.ip_address(parsed.hostname).is_loopback
                or not parsed.port):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("URL must use literal loopback HTTP without credentials, query, or fragment") from None
    return url.rstrip("/")


def signed_headers(body: bytes, invocation_id: str, deadline: str, key: str) -> dict:
    digest = hmac.new(key.encode(), invocation_id.encode() + b"\n" + deadline.encode() + b"\n" + body,
                      hashlib.sha256).hexdigest()
    return {"content-type": "application/json", "x-a2a-id": invocation_id,
            "x-a2a-deadline": deadline, "x-a2a-signature": digest}


def verify_push(body: bytes, headers, key: str):
    try:
        identifier = headers["x-a2a-id"]
        deadline = headers["x-a2a-deadline"]
        if len(body) > MAX_BODY_BYTES or datetime.fromisoformat(deadline) <= datetime.now(timezone.utc):
            raise ValueError()
        expected = signed_headers(body, identifier, deadline, key)["x-a2a-signature"]
        if not hmac.compare_digest(headers["x-a2a-signature"], expected):
            raise ValueError()
        return identifier, deadline
    except (KeyError, ValueError, TypeError):
        raise ValueError("invalid or expired push signature") from None


async def post_signed(url, model, key, *, timeout=None):
    loopback_url(url)
    remaining = (model.deadline - datetime.now(timezone.utc)).total_seconds()
    if timeout is not None:
        remaining = min(remaining, timeout)
    if remaining <= 0:
        raise TimeoutError("invocation expired")
    body = model.model_dump_json().encode()
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("invocation exceeds body limit")
    headers = signed_headers(body, model.turn_id, model.deadline.isoformat(), key)
    async with asyncio.timeout(remaining):
        async with httpx.AsyncClient(timeout=remaining, follow_redirects=False, trust_env=False) as client:
            async with client.stream("POST", url, content=body, headers=headers) as response:
                response.raise_for_status()
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_BODY_BYTES:
                        raise ValueError("response exceeds body limit")
                return bytes(content)


class LoopbackServer:
    """One lifespan on one I/O loop, usable by a blocked synchronous worker."""

    def __init__(self, app):
        self.app = app
        self.loop = None

    def __enter__(self):
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(128)
        self.base_url = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        async def guarded(scope, receive, send):
            if scope["type"] == "http":
                request = Request(scope)
                if (request.headers.get("host") != urlsplit(self.base_url).netloc
                        or request.headers.get("origin", self.base_url) != self.base_url):
                    await JSONResponse({"code": "unsafe_origin"}, status_code=403)(scope, receive, send)
                    return
                messages = []
                size = 0
                while True:
                    message = await receive()
                    size += len(message.get("body", b""))
                    if size > MAX_BODY_BYTES:
                        await JSONResponse({"code": "body_limit"}, status_code=413)(scope, receive, send)
                        return
                    messages.append(message)
                    if not message.get("more_body", False):
                        break

                async def replay():
                    if messages:
                        return messages.pop(0)
                    return await receive()

                await self.app(scope, replay, send)
            else:
                await self.app(scope, receive, send)

        self.server = uvicorn.Server(uvicorn.Config(guarded, log_level="error", access_log=False,
                                                   lifespan="on", timeout_graceful_shutdown=2))

        def serve():
            async def run():
                self.loop = asyncio.get_running_loop()
                await self.server.serve(sockets=[self.socket])
            asyncio.run(run())

        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()
        end = time.monotonic() + 10
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() >= end:
                self.__exit__(None, None, None)
                raise RuntimeError("ASGI server failed to start")
            time.sleep(0.01)
        return self

    def run(self, coroutine, timeout=130):
        future = self.submit(coroutine)
        try:
            return future.result(timeout)
        except BaseException:
            future.cancel()
            raise

    def submit(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop)

    def __exit__(self, *_):
        self.server.should_exit = True
        self.thread.join(10)
        self.socket.close()
        if self.thread.is_alive():
            raise RuntimeError("ASGI server did not stop")


class TurnDispatcher:
    def __init__(self, io: LoopbackServer, *, policy=DELIVERY_POLICY, clock=time.monotonic,
                 request_timeout_s=None):
        self.io = io
        self.policy = policy
        self.clock = clock
        self.request_timeout_s = request_timeout_s

    async def _deliver(self, seat, invocation, deadline, recorder=None):
        def attempt(count):
            if recorder:
                with recorder.lock:
                    if not recorder.closed:
                        recorder.attempts = count

        async def request(remaining):
            body = await post_signed(seat.callback_url + "/turns", invocation, seat.secret,
                                     timeout=min(remaining, self.request_timeout_s) if self.request_timeout_s else remaining)
            completion = TurnCompletion.model_validate_json(body)
            if completion.turn_id != invocation.turn_id or (invocation.kind != "reflect" and completion.reflection is not None):
                raise ValueError("invalid completion")
            return completion

        try:
            completion = await deliver_with_retry(request, deadline=deadline, clock=self.clock,
                                                  policy=self.policy, on_attempt=attempt)
        except DeliveryFailure as exc:
            if recorder:
                recorder.close(exc.closed_by)
            raise
        if recorder:
            recorder.complete(completion)
        return completion

    def dispatch(self, seat, invocation, recorder=None, *, deadline=None):
        if deadline is None:
            deadline = recorder.deadline if recorder else self.clock() + max(
                0, (invocation.deadline - datetime.now(timezone.utc)).total_seconds())
        future = self.io.submit(self._deliver(seat, invocation, deadline, recorder))
        try:
            if recorder is None:
                try:
                    return future.result(max(0, deadline - self.clock()))
                except TimeoutError:
                    raise DeliveryFailure("deadline", 0) from None
            while not recorder.closed:
                recorder.service_work()
                if recorder.remaining() <= 0:
                    recorder.close("deadline")
                elif future.done():
                    try:
                        recorder.complete(future.result())
                    except DeliveryFailure as exc:
                        recorder.close(exc.closed_by)
                if not recorder.closed:
                    recorder.closed_event.wait(min(0.01, recorder.remaining()))
            return recorder.completion
        finally:
            if not future.done():
                future.cancel()
