"""Authenticated /2 seat-scoped staged actions; no game state reaches agents."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pydantic import Field
from a2a_engine.remote.contract import (
    CapabilityClaims,
    ToolOutcome,
    TurnInvocationV2,
    WireModel,
)
from a2a_engine.remote.dispatch import TurnDispatcherV2
from a2a_engine.remote.turns import ToolSpec, TurnRecorder


class TangramAction(WireModel):
    text: str = Field(default="", max_length=16384)
    arrangement: list[str] | None = None
    submit: bool = False
    parse_failed: bool = False


TANGRAM_TOOLS = (
    ToolSpec(
        "submit_tangram",
        "env",
        TangramAction,
        frozenset({"DIRECTOR_ACT", "MATCHER_ACT"}),
        "end_of_turn",
    ),
)


def stage_action(spec, parsed, recorder):
    if any("action" in r for r in recorder.records):
        return recorder.reject("already_submitted", "one action per invocation"), None
    if recorder.invocation.phase == "DIRECTOR_ACT" and (
        parsed.arrangement is not None or parsed.submit
    ):
        return recorder.reject("wrong_role", "only matcher may place or submit"), None
    return ToolOutcome(status="accepted", resolves="end_of_turn"), parsed.model_dump(
        exclude_none=True
    )


class RemoteTangramAgent:
    def __init__(self, runtime, seat_id, timeout_s=120, *, asset_root=None):
        self.runtime, self.seat_id, self.timeout_s = runtime, seat_id, timeout_s
        self.asset_root = asset_root
        self.name = ("director", "matcher")[seat_id]
        self.phase = self.name.upper() + "_ACT"
        self.records = []
        self.dispatcher = TurnDispatcherV2(
            runtime.io,
            runtime.environment.registry.lifecycle,
            network=runtime.environment.network,
        )

    def _act(self, observation):
        from a2a_engine.content import inline_media

        observation = inline_media(observation, self.asset_root)
        ep = self.runtime.episode
        root = f"{self.runtime.io.base_url}/episodes/{ep.episode_id}"
        invocation = TurnInvocationV2(
            environment_id="tangram_reference",
            episode_id=ep.episode_id,
            attempt_id=ep.attempt_id,
            seat=self.seat_id,
            turn_id=uuid.uuid4().hex,
            kind="turn",
            operation="act",
            phase=self.phase,
            observation=observation,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=self.timeout_s),
            mcp={n: f"{root}/{n}/mcp" for n in ("env", "comm", "lifecycle")},
        )
        caps = self.runtime.environment.registry.capabilities
        claims = CapabilityClaims(
            episode_id=ep.episode_id,
            attempt_id=ep.attempt_id,
            seat=self.seat_id,
            turn_id=invocation.turn_id,
            phase=self.phase,
            allowed_tools=["env.submit_tangram"],
            exp=invocation.deadline.timestamp(),
        )
        invocation.capability = caps.mint(claims)
        recorder = TurnRecorder(invocation, claims, caps, stage_action)
        seat = ep.seats[self.seat_id]
        with ep.lock:
            seat.recorder = recorder
        try:
            completion = self.dispatcher.dispatch(seat, invocation, recorder)
        finally:
            records = recorder.close()
            self.records.append(records)
        actions = [r["action"] for r in records if "action" in r]
        if completion is None or len(actions) != 1:
            raise RuntimeError("tangram runtime action failed")
        return actions[0]

    async def act(self, observation, tools):
        return await asyncio.to_thread(self._act, observation)
