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

import httpx
import uvicorn
from starlette.requests import Request
from starlette.responses import JSONResponse

from .contract import MAX_BODY_BYTES, TurnCompletion


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


async def post_signed(url, model, key):
    loopback_url(url)
    remaining = (model.deadline - datetime.now(timezone.utc)).total_seconds()
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
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout)
        except BaseException:
            future.cancel()
            raise

    def __exit__(self, *_):
        self.server.should_exit = True
        self.thread.join(10)
        self.socket.close()
        if self.thread.is_alive():
            raise RuntimeError("ASGI server did not stop")


class TurnDispatcher:
    def __init__(self, io: LoopbackServer):
        self.io = io

    def dispatch(self, seat, invocation):
        remaining = max(0.01, (invocation.deadline - datetime.now(timezone.utc)).total_seconds())
        body = self.io.run(post_signed(seat.callback_url + "/turns", invocation, seat.secret), remaining + 1)
        completion = TurnCompletion.model_validate_json(body)
        if completion.turn_id != invocation.turn_id or completion.reflection is not None:
            raise ValueError("invalid completion for Phase 2 invocation")
        return completion
