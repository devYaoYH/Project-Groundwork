"""Credential-free executable LangGraph transport probe, not a game driver."""

import argparse
import asyncio
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from pydantic import Field
from starlette.responses import JSONResponse
from starlette.routing import Route

from .contract import CapabilityClaims, ToolOutcome, TurnInvocationV2, WireModel
from .dispatch import DeliveryFailure, LoopbackServer, TurnDispatcherV2
from .network import NetworkPolicy
from .seats import write_descriptor
from .server import EnvironmentApp
from .turns import TurnRecorder, ToolSpec


class ProbeInput(WireModel):
    value: str = Field(max_length=32)


class EmptyInput(WireModel):
    pass


def worker_probe(parsed, recorder):
    assert threading.get_ident() == recorder.worker_id
    recorder.worker_calls += 1
    return ToolOutcome(status="ok", resolves="immediate", data={"worker": True})


PROBE_TOOLS = (
    ToolSpec("record_probe", "env", ProbeInput, frozenset({"PROBE"}), "end_of_turn"),
    ToolSpec("worker_probe", "env", EmptyInput, frozenset({"PROBE"}), "immediate", handler=worker_probe),
)


class ProbeState(TypedDict, total=False):
    registered: bool
    completed: bool
    actions: list


@dataclass
class ProbeContext:
    environment: object
    io: object
    episode: object
    deadline_s: float
    recorder: object = None

    def invocation(self, kind):
        root = f"{self.io.base_url}/episodes/{self.episode.episode_id}"
        return TurnInvocationV2(environment_id="probe", episode_id=self.episode.episode_id,
                                attempt_id=self.episode.attempt_id, seat=0, turn_id=uuid.uuid4().hex,
                                kind=kind, operation={"register": "register", "turn": "probe", "episode_end": "episode_end"}[kind],
                                phase="PROBE", deadline=datetime.now(timezone.utc) + timedelta(seconds=self.deadline_s),
                                mcp={name: f"{root}/{name}/mcp" for name in ("env", "comm", "lifecycle")})

    def dispatcher(self):
        return TurnDispatcherV2(self.io, self.environment.registry.lifecycle, request_timeout_s=0.5,
                                network=self.environment.network)


def initialize(state: ProbeState, runtime: Runtime[ProbeContext]):
    ctx = runtime.context
    seat = ctx.episode.wait_ready(0)
    ctx.dispatcher().dispatch(seat, ctx.invocation("register"))
    seat.registered = True
    return {"registered": True}


def agent_turn(state: ProbeState, runtime: Runtime[ProbeContext]):
    ctx = runtime.context
    invocation = ctx.invocation("turn")
    claims = CapabilityClaims(episode_id=invocation.episode_id, attempt_id=invocation.attempt_id, seat=0,
                              turn_id=invocation.turn_id, phase="PROBE", exp=invocation.deadline.timestamp(),
                              allowed_tools=["env.record_probe", "env.worker_probe"])
    capabilities = ctx.environment.registry.capabilities
    invocation.capability = capabilities.mint(claims)

    def stage(spec, parsed, recorder):
        return ToolOutcome(status="accepted", resolves="end_of_turn"), {"value": parsed.value}

    recorder = TurnRecorder(invocation, claims, capabilities, stage)
    recorder.worker_id = threading.get_ident()
    recorder.worker_calls = 0
    ctx.recorder = recorder
    ctx.episode.seats[0].recorder = recorder
    completion = ctx.dispatcher().dispatch(ctx.episode.seats[0], invocation, recorder)
    return {"completed": completion is not None, "actions": recorder.close()}


def finalize(state: ProbeState, runtime: Runtime[ProbeContext]):
    ctx = runtime.context
    if state.get("completed"):
        try:
            ctx.dispatcher().dispatch(ctx.episode.seats[0], ctx.invocation("episode_end"))
        except DeliveryFailure:
            pass
    return {}


def build_probe():
    builder = StateGraph(ProbeState, context_schema=ProbeContext)
    builder.add_node("initialize", initialize)
    builder.add_node("agent_turn", agent_turn)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "agent_turn")
    builder.add_edge("agent_turn", "finalize")
    builder.add_edge("finalize", END)
    return builder.compile()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://runtime-environment:8000")
    parser.add_argument("--agent-origin", default="http://runtime-agent:8001")
    parser.add_argument("--provisioning-dir", type=Path, default=Path("/provision/seat-0"))
    parser.add_argument("--deadline", type=float, default=15)
    args = parser.parse_args()
    network = NetworkPolicy(advertised_origin=args.origin, allowed_origins=[args.origin, args.agent_origin])
    environment = EnvironmentApp(PROBE_TOOLS, network=network)
    public = {"healthy": True, "done": False, "error": None}
    context = None

    async def status(request):
        result = dict(public)
        if context and context.recorder:
            recorder = context.recorder
            with recorder.lock:
                result.update(staged=len(recorder.records), closed=recorder.closed, worker_calls=recorder.worker_calls,
                              snapshot=recorder.snapshot, closed_by=recorder.closed_by)
            records = environment.registry.lifecycle.records
            pending = records.get(environment.registry.lifecycle.key(recorder.invocation))
            result["accepted"] = bool(pending and pending.acceptance and pending.acceptance.status == "accepted")
        return JSONResponse(result)

    async def release(request):
        (args.provisioning_dir / "release").touch(mode=0o600)
        return JSONResponse({"released": True})

    environment.app.routes.extend([Route("/health", status), Route("/status", status),
                                    Route("/release", release, methods=["POST"])])
    with LoopbackServer(environment.app, bind_host="0.0.0.0", port=8000, network=network) as io:
        episode = environment.registry.provision("probe", [0], join_timeout_s=60, environment_id="probe")
        os.chmod(args.provisioning_dir, 0o700)
        descriptor = write_descriptor(args.provisioning_dir, episode, episode.seats[0], io.base_url + "/episodes/probe/join")
        context = ProbeContext(environment, io, episode, args.deadline)

        def run():
            try:
                public["result"] = asyncio.run(build_probe().ainvoke({}, context=context))
            except Exception:
                public["error"] = "probe_failed"
            finally:
                descriptor.unlink(missing_ok=True)
                environment.registry.unregister(episode)
                public["done"] = True

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            while True:
                time.sleep(0.1)
        finally:
            environment.registry.unregister(episode)
            descriptor.unlink(missing_ok=True)
            thread.join(args.deadline + 2)


if __name__ == "__main__":
    main()
