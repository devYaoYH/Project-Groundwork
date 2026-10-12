"""Signed hello/turn callback and private provisioning CLI."""

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import threading
import time
import sys
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
from .runtime_v2 import AgentRuntimeV2
from a2a_engine.remote.joinlink import decode_join_link, MAX_LINK_BYTES
from .service import read_descriptor


class ScriptedRuntime:
    def __init__(self, ticket, *, policy=None, max_cached=128, max_inflight=16, clock=time.monotonic):
        self.ticket = ticket
        self.admission = None
        self.policy = policy if policy is not None else ScriptedPolicy()
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
        completion = TurnCompletion(turn_id=invocation.turn_id, reflection=reflection,
                                    telemetry=getattr(self.policy, "telemetry", {}))
        if len(completion.model_dump_json().encode()) > MAX_BODY_BYTES:
            raise ValueError("completion exceeds body limit")
        if invocation.kind == "register":
            self.registered = True
        if invocation.kind == "episode_end":
            self.ended.set()
        return completion.model_dump(mode="json"), 200


def main():
    parser = argparse.ArgumentParser(description="Run an admitted reference seat")
    parser.add_argument("--join-url", default=os.environ.get("A2A_JOIN_URL"))
    parser.add_argument("--join-descriptor", help="Owner-only runner provisioning descriptor (never log its contents)")
    parser.add_argument("--join-link", action="store_true", help="Read private link from A2A_JOIN_LINK or stdin (not argv)")
    parser.add_argument("--harness", default="scripted", choices=["scripted", "structured_output"])
    parser.add_argument("--environment-id", default="calendar", choices=["calendar", "word_guess", "buyer_seller", "negotiation"])
    parser.add_argument("--model-config", help="Nonsecret model configuration JSON file")
    parser.add_argument("--port", type=int, default=0, help="Loopback callback port (default: ephemeral)")
    args, unknown = parser.parse_known_args()
    if unknown:
        parser.error("unexpected arguments; private join links must come from stdin or A2A_JOIN_LINK")
    ticket = os.environ.get("A2A_JOIN_TICKET")
    if args.join_link and args.join_descriptor:
        parser.error("choose one private provisioning source")
    if args.join_link:
        try:
            link = os.environ.get("A2A_JOIN_LINK")
            if link is None:
                link = sys.stdin.read(MAX_LINK_BYTES + 2).removesuffix("\n")
            descriptor = decode_join_link(link)
            args.join_url, ticket = descriptor["join_url"], descriptor["join_ticket"]
            args.environment_id = descriptor["environment_id"]
        except (OSError, ValueError):
            parser.error("invalid private join link")
    if args.join_descriptor:
        try:
            descriptor = read_descriptor(args.join_descriptor)
            args.join_url, ticket = descriptor["join_url"], descriptor["join_ticket"]
            args.environment_id = descriptor["environment_id"]
            if not isinstance(args.join_url, str) or not isinstance(ticket, str):
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError):
            parser.error("join descriptor must be valid owner-only provisioning in a regular file")
    if not args.join_url or not ticket:
        parser.error("join URL and private A2A_JOIN_TICKET are required")
    if args.environment_id not in {"calendar", "word_guess", "buyer_seller", "negotiation"}:
        parser.error("unsupported provisioning environment")
    if args.environment_id == 'negotiation':
        from negotiation_game.runtime_strategy import NegotiationStrategy
        config = None
        if args.harness == 'structured_output':
            try:
                config = json.loads(os.environ.get('A2A_MODEL_CONFIG', '{}'))
                if args.model_config:
                    with open(args.model_config) as handle:
                        config = json.load(handle)
                if not isinstance(config, dict) or not config.get('model') or 'api_key' in config:
                    raise ValueError()
                config['type'] = 'llm'
            except (OSError, ValueError, TypeError):
                parser.error('Negotiation requires nonsecret backend model configuration')
        runtime = AgentRuntimeV2()
        with LoopbackServer(runtime.app, port=args.port) as server:
            admission = server.run(runtime.join(args.join_url, server.base_url, ticket,
                                                policy=NegotiationStrategy(config=config)))
            key = (admission.environment_id, admission.episode_id, admission.attempt_id, admission.seat)
            while not runtime.sessions[key].ended:
                time.sleep(0.05)
        return
    policy = None
    if args.harness == "structured_output":
        from a2a_engine.llm.factory import make_llm_client
        from .strategies import StructuredOutputStrategy
        try:
            config = json.loads(os.environ.get("A2A_MODEL_CONFIG", "{}"))
            if args.model_config:
                with open(args.model_config) as handle:
                    config = json.load(handle)
            if not isinstance(config, dict) or not config.get("model") or "api_key" in config:
                raise ValueError()
            policy = StructuredOutputStrategy(make_llm_client({"temperature": 0.0, **config}))
        except (OSError, ValueError, TypeError):
            parser.error("structured_output requires nonsecret model configuration")
    if args.environment_id in {"word_guess", "buyer_seller"}:
        if args.environment_id == "word_guess":
            from word_guess.runtime_strategy import WordGuessStrategy as Strategy
        else:
            from buyer_seller.runtime_strategy import BuyerSellerStrategy as Strategy
        client = policy.model if policy is not None else None
        runtime = AgentRuntimeV2()
        with LoopbackServer(runtime.app, port=args.port) as server:
            admission = server.run(runtime.join(args.join_url, server.base_url, ticket,
                                                policy=Strategy(client=client)))
            key = (admission.environment_id, admission.episode_id, admission.attempt_id, admission.seat)
            while not runtime.sessions[key].ended:
                time.sleep(0.05)
        return
    runtime = ScriptedRuntime(ticket, policy=policy)
    with LoopbackServer(runtime.app, port=args.port) as server:
        server.run(runtime.join(args.join_url, server.base_url))
        runtime.ended.wait()


if __name__ == "__main__":
    main()
