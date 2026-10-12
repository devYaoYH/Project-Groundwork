"""Pre-stage Calendar lifecycle retained only for frozen-driver parity."""

import dataclasses
import uuid
from datetime import datetime, timedelta, timezone

from a2a_engine.remote.contract import PROTOCOL_VERSION_V2, TurnInvocation, TurnInvocationV2
from a2a_engine.remote.dispatch import DeliveryFailure
from calendar_game.prompts import build_system_prompt
from calendar_game.remote import CALENDAR_TOOLS, RemoteSeatClient


class LegacyRemoteSeatClient(RemoteSeatClient):
    def _invocation(self, kind, **kwargs):
        if kind not in {"register", "episode_end"}:
            return super()._invocation(kind, **kwargs)
        episode = self.runtime.episode
        root = f"{self.runtime.io.base_url}/episodes/{episode.episode_id}"
        v2 = self.seat.protocol_version == PROTOCOL_VERSION_V2
        extra = {"environment_id": episode.environment_id, "attempt_id": episode.attempt_id,
                 "operation": kind} if v2 else {}
        return (TurnInvocationV2 if v2 else TurnInvocation)(episode_id=episode.episode_id,
            seat=self.seat.seat, turn_id=uuid.uuid4().hex, kind=kind, **extra,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=self._timeout(kind)),
            mcp={name: f"{root}/{name}/mcp" for name in (("env", "comm", "lifecycle") if v2 else ("env", "comm"))},
            **kwargs)

    def register(self, agent_id, game_config):
        if agent_id != self.seat.seat:
            raise ValueError("remote seat identity mismatch")
        self.runtime.episode.wait_ready(agent_id)
        self._select_dispatcher()
        self.config = game_config
        config = dataclasses.asdict(game_config)
        invocation = self._invocation("register", observation={
            "game_config": config,
            "action_schema": {spec.name: spec.input_model.model_json_schema() for spec in CALENDAR_TOOLS},
            "action_to_tool": {"dm": "comm.send", "participant_groupchat": "comm.send",
                               "all_groupchat": "comm.send", "schedule": "env.schedule",
                               "reschedule": "env.reschedule"},
        }, prompt=build_system_prompt(config))
        try:
            self.dispatcher.dispatch(self.seat, invocation, deadline=self.clock() + self._timeout("register"))
        except DeliveryFailure:
            raise TimeoutError("seat_unavailable") from None
        with self.runtime.episode.lock:
            self.seat.registered = True

    def episode_end(self):
        if self.seat.registered and self.runtime.episode.active:
            try:
                self.dispatcher.dispatch(self.seat, self._invocation("episode_end"),
                                         deadline=self.clock() + self._timeout("episode_end"))
            except Exception:
                pass
