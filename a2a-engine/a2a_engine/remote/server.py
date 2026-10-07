"""Episode registry and two fixed MCP surfaces sharing one app lifespan."""

import secrets
import threading
import time
from contextlib import AsyncExitStack, asynccontextmanager

from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from .admission import join
from .capabilities import AuthorizationError, Capabilities
from .contract import JoinRequest, ToolOutcome
from .seats import EpisodeContext


class EpisodeRegistry:
    def __init__(self):
        self.capabilities = Capabilities()
        self._episodes = {}
        self._lock = threading.RLock()

    def provision(self, episode_id, seats, *, join_timeout_s=10):
        if not episode_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in episode_id):
            raise ValueError("episode ID must be a URL-safe identifier")
        context = EpisodeContext(episode_id, seats, join_timeout_s=join_timeout_s)
        with self._lock:
            if episode_id in self._episodes:
                raise ValueError("episode is already registered")
            self._episodes[episode_id] = context
        return context

    def get(self, episode_id):
        with self._lock:
            context = self._episodes.get(episode_id)
            if context is None or not context.active:
                raise AuthorizationError("episode is unavailable")
            return context

    def unregister(self, context):
        with self._lock, context.lock:
            if self._episodes.get(context.episode_id) is not context:
                return
            context.active = False
            for seat in context.seats.values():
                if seat.recorder:
                    seat.recorder.close()
                seat.secret = None
                seat.ticket = ""
                seat.ready.clear()
            del self._episodes[context.episode_id]


class EnvironmentApp:
    def __init__(self, specs):
        self.registry = EpisodeRegistry()
        self.specs = {(spec.endpoint, spec.name): spec for spec in specs}
        self.managers = {}
        for endpoint in ("env", "comm"):
            server = Server(f"a2a-{endpoint}")
            self._bind(server, endpoint)
            self.managers[endpoint] = StreamableHTTPSessionManager(server, stateless=True, json_response=True)

        @asynccontextmanager
        async def lifespan(app):
            async with AsyncExitStack() as stack:
                for manager in self.managers.values():
                    await stack.enter_async_context(manager.run())
                yield

        outer = self

        class McpRoute:
            async def __call__(self, scope, receive, send):
                params = scope["path_params"]
                try:
                    outer.registry.get(params["episode_id"])
                    manager = outer.managers[params["endpoint"]]
                except (AuthorizationError, KeyError):
                    await JSONResponse({"code": "unavailable_endpoint"}, status_code=404)(scope, receive, send)
                    return
                await manager.handle_request(scope, receive, send)

        self.app = Starlette(lifespan=lifespan, routes=[
            Route("/episodes/{episode_id}/join", self._join, methods=["POST"]),
            Route("/episodes/{episode_id}/ready", self._ready, methods=["POST"]),
            Route("/episodes/{episode_id}/{endpoint}/mcp", McpRoute(), methods=["POST", "GET", "DELETE"]),
        ])

    def _bind(self, server, endpoint):
        @server.list_tools()
        async def list_tools():
            return [Tool(name=spec.name, description=f"Resolves: {spec.resolves}",
                         inputSchema=spec.input_model.model_json_schema())
                    for spec in self.specs.values() if spec.endpoint == endpoint]

        @server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            try:
                request_context = server.request_context
                request = request_context.request
                episode = self.registry.get(request.path_params["episode_id"])
                authorization = request.headers.get("authorization", "")
                if not authorization.startswith("Bearer "):
                    raise AuthorizationError("capability required")
                claims = self.registry.capabilities.verify(authorization[7:], episode_id=episode.episode_id,
                                                          attempt_id=episode.attempt_id)
                with episode.lock:
                    seat = episode.seats.get(claims.seat)
                    if (seat is None or not seat.registered or not seat.ready.is_set()
                            or seat.recorder is None or seat.recorder.claims != claims):
                        raise AuthorizationError("seat has no matching registered open turn")
                    recorder = seat.recorder
                if f"{endpoint}.{name}" not in claims.allowed_tools:
                    raise AuthorizationError("tool is not granted")
                meta = request_context.meta.model_dump() if request_context.meta else {}
                outcome = recorder.call(self.specs.get((endpoint, name)), arguments, meta.get("a2a/call_id"))
            except AuthorizationError:
                outcome = ToolOutcome(status="rejected", resolves="read_only", code="unauthorized",
                                      reason="capability is not authorized for this call")
            return CallToolResult(content=[TextContent(type="text", text=outcome.model_dump_json())],
                                  structuredContent=outcome.model_dump(mode="json"),
                                  isError=outcome.status == "rejected")

    async def _join(self, request):
        try:
            context = self.registry.get(request.path_params["episode_id"])
            parsed = JoinRequest.model_validate_json(await request.body())
            result = await join(context, parsed, str(request.base_url).rstrip("/"))
            return JSONResponse(result.model_dump(mode="json"))
        except (ValueError, ValidationError, TimeoutError):
            return JSONResponse({"code": "admission_rejected", "message": "invalid admission or callback"}, status_code=400)
        except Exception:
            return JSONResponse({"code": "callback_unavailable"}, status_code=400)

    async def _ready(self, request):
        try:
            context = self.registry.get(request.path_params["episode_id"])
            token = request.headers.get("authorization", "").removeprefix("Bearer ")
            with context.lock:
                seat = next((s for s in context.seats.values() if s.secret and secrets.compare_digest(s.secret, token)), None)
                if seat is None or time.time() >= seat.join_deadline:
                    raise AuthorizationError("invalid ready acknowledgement")
                seat.ready.set()
            return JSONResponse({"ready": True})
        except AuthorizationError:
            return JSONResponse({"code": "unauthorized"}, status_code=401)
