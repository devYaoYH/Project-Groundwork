"""Word Guess action staging: successful completion AND one text are required."""

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

from pydantic import Field

from a2a_engine.remote.contract import CapabilityClaims, ToolOutcome, TurnInvocationV2, WireModel
from a2a_engine.remote.dispatch import TurnDispatcherV2
from a2a_engine.remote.turns import ToolSpec, TurnRecorder

class TextAction(WireModel):
    text: str = Field(max_length=16384)


WORD_GUESS_TOOLS = (ToolSpec("submit_action", "env", TextAction, frozenset({"ACT"}), "end_of_turn"),)


def stage_text(spec, parsed, recorder):
    if any("action" in record for record in recorder.records):
        return recorder.reject("already_submitted", "one action per invocation"), None
    return ToolOutcome(status="accepted", resolves="end_of_turn"), parsed.model_dump()


class RemoteTextAgent:
    def __init__(self, runtime, seat_id, *, timeout_s=120):
        self.runtime, self.seat_id, self.timeout_s = runtime, seat_id, timeout_s
        self.name = "guesser" if seat_id == 0 else "host"
        self.records = []
        self.dispatcher = TurnDispatcherV2(runtime.io, runtime.environment.registry.lifecycle,
                                           network=runtime.environment.network)

    def invocation(self, kind, observation=None, prompt=None):
        episode = self.runtime.episode
        root = f"{self.runtime.io.base_url}/episodes/{episode.episode_id}"
        return TurnInvocationV2(environment_id="word_guess", episode_id=episode.episode_id,
                                attempt_id=episode.attempt_id, seat=self.seat_id, turn_id=uuid.uuid4().hex,
                                kind=kind, operation="act",
                                phase="ACT" if kind == "turn" else None,
                                observation=observation or {}, prompt=prompt,
                                deadline=datetime.now(timezone.utc) + timedelta(seconds=self.timeout_s),
                                mcp={name: f"{root}/{name}/mcp" for name in ("env", "comm", "lifecycle")})

    def _act(self, observation):
        invocation = self.invocation("turn", observation)
        capabilities = self.runtime.environment.registry.capabilities
        claims = CapabilityClaims(episode_id=invocation.episode_id, attempt_id=invocation.attempt_id,
                                  seat=self.seat_id, turn_id=invocation.turn_id, phase="ACT",
                                  allowed_tools=["env.submit_action"], exp=invocation.deadline.timestamp())
        invocation.capability = capabilities.mint(claims)
        recorder = TurnRecorder(invocation, claims, capabilities, stage_text)
        seat = self.runtime.episode.seats[self.seat_id]
        with self.runtime.episode.lock:
            seat.recorder = recorder
        try:
            completion = self.dispatcher.dispatch(seat, invocation, recorder)
        finally:
            records = recorder.close()
            self.records.append(records)
        actions = [record["action"] for record in records if "action" in record]
        if completion is None or len(actions) != 1:
            raise RuntimeError("word_guess runtime action failed") from None
        return actions[0]

    async def act(self, observation, tools):
        return await asyncio.to_thread(self._act, observation)
