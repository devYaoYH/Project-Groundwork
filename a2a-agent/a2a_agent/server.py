"""Signed hello/turn callback and private provisioning CLI."""

import argparse
import asyncio
import hashlib
import inspect
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_engine.remote.contract import MAX_BODY_BYTES, Hello, JoinRequest, JoinResponse, PROTOCOL_VERSION, TurnCompletion, TurnInvocation
from a2a_engine.remote.dispatch import LoopbackServer, loopback_url, verify_push
from .mcp_client import execute
from .scripted import ScriptedPolicy


class ScriptedRuntime:
    def __init__(self, ticket, *, max_cached=128, max_inflight=16, clock=time.monotonic):
        self.ticket = ticket
        self.admission = None
        self.policy = ScriptedPolicy()
        self.registered = False
        self.ended = threading.Event()
        self.lock = asyncio.Lock()
        self.completed = OrderedDict()
        self.inflight = {}
        self.max_cached = max_cached
        self.max_inflight = max_inflight
        self.clock = clock
        self.app = Starlette(routes=[Route("/hello", self.hello, methods=["POST"]),
                                     Route("/turns", self.turn, methods=["POST"])])

    async def hello(self, request):
        try:
            body = await request.body()
            identifier, deadline = verify_push(body, request.headers, self.ticket)
            hello = Hello.model_validate_json(body)
            if hello.turn_id != identifier or hello.deadline.isoformat() != deadline or self.admission:
                raise ValueError()
            return JSONResponse({"nonce": hello.nonce, "accept": True})
        except ValueError:
            return JSONResponse({"code": "invalid_hello"}, status_code=401)

    async def join(self, join_url, callback_url):
        loopback_url(join_url)
        loopback_url(callback_url)
        request = JoinRequest(join_ticket=self.ticket, callback_url=callback_url,
                              protocol_versions=[PROTOCOL_VERSION], agent_info={"name": "a2a-agent", "version": "0.1.0"})
        async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
            result = await client.post(join_url, json=request.model_dump())
            result.raise_for_status()
            self.admission = JoinResponse.model_validate(result.json())
            loopback_url(self.admission.ready_url)
            result = await client.post(self.admission.ready_url,
                                       headers={"authorization": f"Bearer {self.admission.seat_secret}"})
            result.raise_for_status()
        return self.admission

    async def turn(self, request):
        try:
            if not self.admission:
                raise ValueError()
            body = await request.body()
            identifier, deadline = verify_push(body, request.headers, self.admission.seat_secret)
            invocation = TurnInvocation.model_validate_json(body)
            if (invocation.turn_id != identifier or invocation.deadline.isoformat() != deadline
                    or invocation.episode_id != self.admission.episode_id or invocation.seat != self.admission.seat
                    or invocation.mcp != self.admission.mcp
                    or (invocation.kind != "turn" and invocation.capability is not None)):
                raise ValueError()
        except ValueError:
            return JSONResponse({"code": "invalid_push"}, status_code=401)
        digest = hashlib.sha256(body).hexdigest()
        self._expire()
        if identifier in self.completed:
            previous, _expiry, response, status = self.completed[identifier]
            if previous != digest:
                return JSONResponse({"code": "invocation_conflict"}, status_code=409)
            return JSONResponse(response, status_code=status)
        if identifier in self.inflight:
            previous, task = self.inflight[identifier]
            if previous != digest:
                return JSONResponse({"code": "invocation_conflict"}, status_code=409)
        else:
            if self.ended.is_set():
                return JSONResponse({"code": "episode_ended"}, status_code=409)
            if len(self.inflight) >= self.max_inflight or len(self.completed) + len(self.inflight) >= self.max_cached:
                return JSONResponse({"code": "invocation_capacity"}, status_code=503)
            remaining = (invocation.deadline - datetime.now(timezone.utc)).total_seconds()
            expiry = self.clock() + max(0, remaining)
            task = asyncio.create_task(self._execute(invocation, digest, expiry))
            self.inflight[identifier] = (digest, task)
        response, status = await asyncio.shield(task)
        return JSONResponse(response, status_code=status)

    def _expire(self):
        for identifier, (_digest, expiry, _response, _status) in list(self.completed.items()):
            if expiry <= self.clock():
                del self.completed[identifier]

    async def _execute(self, invocation, digest, expiry):
        identifier = invocation.turn_id
        try:
            async with asyncio.timeout(max(0, expiry - self.clock())):
                async with self.lock:
                    response, status = await self._run(invocation)
        except TimeoutError:
            response, status = {"code": "expired"}, 408
        except Exception:
            response, status = {"code": "execution_failed"}, 500
        finally:
            self.inflight.pop(identifier, None)
        self.completed[identifier] = (digest, expiry, response, status)
        self._expire()
        return response, status

    async def _run(self, invocation):
        if invocation.kind != "register" and not self.registered:
            return {"code": "registration_required"}, 409
        if invocation.kind == "register" and self.registered:
            return {"code": "already_registered"}, 409
        if invocation.kind == "turn" and (
                not invocation.capability or invocation.phase not in {"CHEAP_TALK", "DECISION", "VOLUNTARY", "DECISION_RETRY"}
                or (invocation.phase == "DECISION_RETRY" and invocation.parent_phase not in {"DECISION", "VOLUNTARY"})):
            return {"code": "unsupported_turn"}, 400
        reflection = None
        if invocation.kind == "reflect":
            hook = getattr(self.policy, "reflect", None)
            reflection = hook(invocation) if hook else None
            if inspect.isawaitable(reflection):
                reflection = await reflection
        else:
            calls = self.policy.calls(invocation)
            if inspect.isawaitable(calls):
                calls = await calls
            if calls:
                if not invocation.capability:
                    raise ValueError("action capability required")
                await execute(invocation, calls)
        completion = TurnCompletion(turn_id=invocation.turn_id, reflection=reflection)
        if len(completion.model_dump_json().encode()) > MAX_BODY_BYTES:
            raise ValueError("completion exceeds body limit")
        if invocation.kind == "register":
            self.registered = True
        if invocation.kind == "episode_end":
            self.ended.set()
        return completion.model_dump(mode="json"), 200


def main():
    parser = argparse.ArgumentParser(description="Run an external scripted seat")
    parser.add_argument("--join-url", default=os.environ.get("A2A_JOIN_URL"))
    args = parser.parse_args()
    ticket = os.environ.get("A2A_JOIN_TICKET")
    if not args.join_url or not ticket:
        parser.error("join URL and private A2A_JOIN_TICKET are required")
    runtime = ScriptedRuntime(ticket)
    with LoopbackServer(runtime.app) as server:
        server.run(runtime.join(args.join_url, server.base_url))
        runtime.ended.wait()


if __name__ == "__main__":
    main()
