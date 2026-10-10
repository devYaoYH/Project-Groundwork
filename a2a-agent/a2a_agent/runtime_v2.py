"""Long-lived acknowledgement-first MCP runtime with isolated seat sessions."""

import asyncio
import hashlib
import inspect
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from a2a_engine.remote.contract import (
    MAX_BODY_BYTES, HelloV2, JoinRequest, JoinResponseV2, PROTOCOL_VERSION_V2,
    TurnAcceptance, TurnFinished, TurnInvocationV2,
)
from a2a_engine.remote.dispatch import verify_push
from a2a_engine.remote.network import NetworkPolicy
from .mcp_client import execute, report_completion
from .scripted import ScriptedPolicy


@dataclass
class Session:
    admission: JoinResponseV2
    policy: object
    expires: float
    active: str | None = None
    registered: bool = False
    ended: bool = False


@dataclass
class InvocationRecord:
    digest: str
    acceptance: TurnAcceptance
    expires: float
    session: Session
    task: object = None
    finished: TurnFinished | None = None
    receipt: object = None


class AgentRuntimeV2:
    def __init__(self, *, network=None, max_sessions=128, max_records=1024,
                 max_active=128, grace_s=5, session_ttl_s=3600, clock=time.monotonic):
        self.network = network or NetworkPolicy()
        self.max_sessions = max_sessions
        self.max_records = max_records
        self.max_active = max_active
        self.grace_s = grace_s
        self.session_ttl_s = session_ttl_s
        self.clock = clock
        self.sessions = {}
        self.records = OrderedDict()
        self.bootstrap = {}
        self.stopping = False
        self.server = Server("a2a-agent-v2")
        self.manager = StreamableHTTPSessionManager(self.server, stateless=True, json_response=True)
        self._bind()
        outer = self

        class McpRoute:
            async def __call__(self, scope, receive, send):
                await outer.manager.handle_request(scope, receive, send)

        @asynccontextmanager
        async def lifespan(app):
            async with self.manager.run():
                yield
                await self.close()

        self.app = Starlette(lifespan=lifespan, routes=[
            Route("/hello", self.hello, methods=["POST"]),
            Route("/mcp", McpRoute(), methods=["POST", "GET", "DELETE"]),
            Route("/health", self.health),
        ])

    async def health(self, request):
        self._expire()
        return JSONResponse({"healthy": not self.stopping})

    async def hello(self, request):
        body = await request.body()
        for ticket, (expiry, previous) in list(self.bootstrap.items()):
            if expiry <= self.clock():
                continue
            try:
                identifier, deadline = verify_push(body, request.headers, ticket)
                hello = HelloV2.model_validate_json(body)
                if hello.turn_id != identifier or hello.deadline.isoformat() != deadline or previous is not None:
                    continue
                self.bootstrap[ticket] = (expiry, hello)
                return JSONResponse({"nonce": hello.nonce, "accept": True})
            except ValueError:
                continue
        return JSONResponse({"code": "invalid_hello"}, status_code=401)

    async def join(self, join_url, callback_url, ticket, *, policy=None):
        self._expire()
        if len(self.sessions) + len(self.bootstrap) >= self.max_sessions:
            raise ValueError("session capacity exceeded")
        self.network.validate(join_url)
        self.network.validate(callback_url)
        self.bootstrap[ticket] = (self.clock() + 10, None)
        try:
            request = JoinRequest(join_ticket=ticket, callback_url=callback_url,
                                  protocol_versions=[PROTOCOL_VERSION_V2], agent_info={"name": "a2a-agent-v2"})
            async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
                response = await client.post(join_url, json=request.model_dump())
                response.raise_for_status()
                if len(response.content) > MAX_BODY_BYTES:
                    raise ValueError("admission body limit")
                admission = JoinResponseV2.model_validate(response.json())
                hello = self.bootstrap[ticket][1]
                if hello is None or any(getattr(hello, key) != getattr(admission, key) for key in (
                        "environment_id", "episode_id", "attempt_id", "seat")):
                    raise ValueError("admission identity mismatch")
                expected_root = join_url.removesuffix("/join")
                if admission.mcp != {name: f"{expected_root}/{name}/mcp" for name in ("env", "comm", "lifecycle")}:
                    raise ValueError("admission endpoints mismatch")
                if admission.ready_url != expected_root + "/ready":
                    raise ValueError("admission ready URL mismatch")
                self.network.validate(admission.ready_url)
                key = (admission.environment_id, admission.episode_id, admission.attempt_id, admission.seat)
                session = Session(admission, policy if policy is not None else ScriptedPolicy(),
                                  self.clock() + self.session_ttl_s)
                self.sessions[key] = session
                response = await client.post(admission.ready_url,
                                             headers={"authorization": f"Bearer {admission.seat_secret}"})
                response.raise_for_status()
                return admission
        except BaseException:
            if "key" in locals():
                self.sessions.pop(key, None)
            raise
        finally:
            self.bootstrap.pop(ticket, None)

    def _bind(self):
        @self.server.list_tools()
        async def tools():
            return [Tool(name="start_turn", description="Admit a turn, not its completion",
                         inputSchema=TurnInvocationV2.model_json_schema())]

        @self.server.call_tool(validate_input=False)
        async def call(name, arguments):
            try:
                invocation = TurnInvocationV2.model_validate(arguments)
                if len(invocation.model_dump_json().encode()) > MAX_BODY_BYTES or name != "start_turn":
                    raise ValueError()
                credential = self.server.request_context.request.headers.get("authorization", "").removeprefix("Bearer ")
                acceptance = self.start(invocation, credential)
                return CallToolResult(content=[TextContent(type="text", text=acceptance.model_dump_json())],
                                      structuredContent=acceptance.model_dump(mode="json"))
            except ValueError:
                return CallToolResult(content=[TextContent(type="text", text='{"code":"invalid_invocation"}')],
                                      structuredContent={"code": "invalid_invocation"}, isError=True)

    def _expire(self):
        now = self.clock()
        for key, record in list(self.records.items()):
            if record.finished is not None and record.expires <= now and record.task.done():
                del self.records[key]
        for key, session in list(self.sessions.items()):
            if session.active is None and session.expires <= now and not self._provider_busy(session):
                del self.sessions[key]

    @staticmethod
    def _provider_busy(session):
        lock = getattr(session.policy, "model_lock", None)
        return lock is not None and lock.locked()

    def start(self, invocation, credential):
        import secrets
        self._expire()
        identity = invocation.identity().model_dump()
        key = tuple(identity[name] for name in ("environment_id", "episode_id", "attempt_id", "seat"))
        session = self.sessions.get(key)
        if session is None or not secrets.compare_digest(session.admission.seat_secret, credential):
            raise ValueError("invalid session credential")
        if invocation.mcp != session.admission.mcp or not invocation.lifecycle_credential:
            raise ValueError("invalid invocation endpoints")
        if invocation.kind != "turn" and invocation.capability is not None:
            raise ValueError("unexpected action authority")
        if invocation.kind == "turn" and not invocation.capability:
            raise ValueError("action authority required")
        if invocation.environment_id == "calendar" and invocation.kind == "turn" and (
                invocation.phase not in {"CHEAP_TALK", "DECISION", "VOLUNTARY", "DECISION_RETRY"}
                or invocation.phase == "DECISION_RETRY" and invocation.parent_phase not in {"DECISION", "VOLUNTARY"}):
            raise ValueError("invalid action phase")
        if invocation.environment_id == 'negotiation':
            from negotiation_game.remote import OPERATIONS
            valid = (invocation.kind == 'turn' and invocation.operation in OPERATIONS
                     and invocation.phase == invocation.operation.upper() and invocation.parent_phase is None)
            valid = valid or (invocation.kind in {'register', 'episode_end'} and invocation.operation == invocation.kind)
            if not valid:
                raise ValueError('invalid Negotiation operation')
        elif invocation.operation != {"register": "register", "round_start": "observe", "turn": "act",
                                    "reflect": "reflect", "episode_end": "episode_end"}[invocation.kind]:
            if not (invocation.kind == "turn" and invocation.operation == "probe"
                    and invocation.environment_id == "probe"):
                raise ValueError("invalid operation")
        record_key = (*key, invocation.turn_id)
        digest = hashlib.sha256(invocation.model_dump_json().encode()).hexdigest()
        if record_key in self.records:
            record = self.records[record_key]
            if record.digest == digest:
                return record.acceptance.model_copy(deep=True)
            return TurnAcceptance(**identity, status="rejected", reason_code="conflict")
        remaining = (invocation.deadline - datetime.now(timezone.utc)).total_seconds()
        reason = None
        if self.stopping or session.ended:
            reason = "ended"
        elif remaining <= 0:
            reason = "expired"
        elif session.active is not None or self._provider_busy(session):
            reason = "busy"
        elif len(self.records) >= self.max_records or sum(s.active is not None for s in self.sessions.values()) >= self.max_active:
            reason = "capacity"
        elif (invocation.kind == "register") == session.registered:
            reason = "registration_required"
        if reason:
            return TurnAcceptance(**identity, status="rejected", reason_code=reason)
        acceptance = TurnAcceptance(**identity, status="accepted")
        record = InvocationRecord(digest, acceptance, self.clock() + remaining + self.grace_s, session)
        session.expires = max(session.expires, self.clock() + remaining + self.grace_s)
        session.active = invocation.turn_id
        self.records[record_key] = record
        record.task = asyncio.create_task(self._execute(invocation.model_copy(deep=True), record))
        return acceptance.model_copy(deep=True)

    async def _execute(self, invocation, record):
        session = record.session
        reflection = None
        status, failure = "succeeded", None
        try:
            async with asyncio.timeout(max(0, (invocation.deadline - datetime.now(timezone.utc)).total_seconds())):
                if invocation.kind == "reflect":
                    hook = getattr(session.policy, "reflect", None)
                    reflection = hook(invocation) if hook else None
                    if inspect.isawaitable(reflection):
                        reflection = await reflection
                else:
                    calls = session.policy.calls(invocation)
                    if inspect.isawaitable(calls):
                        calls = await calls
                    if calls:
                        if not invocation.capability:
                            raise ValueError("action authority required")
                        await execute(invocation, calls, network=self.network)
        except TimeoutError:
            status, failure = "failed", "deadline"
        except asyncio.CancelledError:
            status, failure = "failed", "cancelled"
        except Exception:
            status, failure = "failed", "execution_failed"
        try:
            finished = TurnFinished(**invocation.identity().model_dump(), status=status, failure_code=failure,
                                    telemetry=getattr(session.policy, "telemetry", {}), reflection=reflection).model_copy(deep=True)
            if len(finished.model_dump_json().encode()) > MAX_BODY_BYTES:
                raise ValueError("terminal body limit")
        except Exception:
            finished = TurnFinished(**invocation.identity().model_dump(), status="failed", failure_code="execution_failed")
        # History/action work is over. Publish immutable terminal state and release
        # execution BEFORE awaiting the receipt: the environment may already dispatch again.
        record.finished = finished
        if finished.status == "succeeded" and invocation.kind == "register":
            session.registered = True
        if invocation.kind == "episode_end":
            session.ended = True
            session.expires = self.clock() + self.grace_s
        session.active = None
        try:
            record.receipt = await report_completion(invocation, finished, network=self.network, grace_s=self.grace_s)
        except (Exception, asyncio.CancelledError):
            pass

    async def close(self):
        self.stopping = True
        tasks = [record.task for record in self.records.values() if not record.task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.sessions.clear()
        self.bootstrap.clear()
        self.records.clear()
