"""Signed hello/turn callback and private provisioning CLI."""

import argparse
import asyncio
import hashlib
import os
import threading
from collections import OrderedDict
from datetime import datetime, timezone

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_engine.remote.contract import Hello, JoinRequest, JoinResponse, PROTOCOL_VERSION, TurnCompletion, TurnInvocation
from a2a_engine.remote.dispatch import LoopbackServer, loopback_url, verify_push
from .mcp_client import execute
from .scripted import ScriptedPolicy


class ScriptedRuntime:
    def __init__(self, ticket):
        self.ticket = ticket
        self.admission = None
        self.policy = ScriptedPolicy()
        self.registered = False
        self.ended = threading.Event()
        self.lock = asyncio.Lock()
        self.completed = OrderedDict()
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
            if not self.admission or self.ended.is_set():
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
        async with self.lock:
            if identifier in self.completed:
                previous, completion = self.completed[identifier]
                if previous != digest:
                    return JSONResponse({"code": "invocation_conflict"}, status_code=409)
                return JSONResponse(completion)
            if invocation.kind != "register" and not self.registered:
                return JSONResponse({"code": "registration_required"}, status_code=409)
            if invocation.kind == "register" and self.registered:
                return JSONResponse({"code": "already_registered"}, status_code=409)
            if invocation.kind == "reflect":
                return JSONResponse({"code": "unsupported", "message": "reflection requires Phase 3"}, status_code=400)
            if invocation.kind == "turn" and (not invocation.capability or invocation.phase not in {"CHEAP_TALK", "DECISION"}):
                return JSONResponse({"code": "unsupported_turn"}, status_code=400)
            remaining = (invocation.deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                return JSONResponse({"code": "expired"}, status_code=401)
            try:
                async with asyncio.timeout(remaining):
                    calls = self.policy.calls(invocation)
                    if calls:
                        if not invocation.capability:
                            raise ValueError("action capability required")
                        await execute(invocation, calls)
            except (ValueError, TimeoutError):
                return JSONResponse({"code": "unsupported_or_expired"}, status_code=400)
            if invocation.kind == "register":
                self.registered = True
            if invocation.kind == "episode_end":
                self.ended.set()
            completion = TurnCompletion(turn_id=identifier).model_dump(mode="json")
            self.completed[identifier] = (digest, completion)
            while len(self.completed) > 128:
                self.completed.popitem(last=False)
            return JSONResponse(completion)


def main():
    parser = argparse.ArgumentParser(description="Run a Phase 2 external scripted seat")
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
