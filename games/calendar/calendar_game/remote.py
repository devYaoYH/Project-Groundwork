"""Schedule-only remote BaseClient; the calendar worker remains authoritative."""

import dataclasses
import uuid
from datetime import datetime, timedelta, timezone

from pydantic import Field, StrictInt

from a2a_engine.comm import RoutingContext
from a2a_engine.remote.contract import CapabilityClaims, ToolOutcome, TurnInvocation, WireModel
from a2a_engine.remote.dispatch import TurnDispatcher
from a2a_engine.remote.turns import ToolSpec, TurnRecorder
from calendar_game.agents import BaseClient, DecideResult, TurnResult
from calendar_game.prompts import build_round_start_message, build_system_prompt


class EmptyInput(WireModel):
    pass


class SendInput(WireModel):
    channel: str = Field(max_length=64)
    content: str = Field(max_length=16384)
    to: StrictInt | None = None


class ScheduleInput(WireModel):
    meeting_id: StrictInt
    slot: StrictInt
    justification: str | None = Field(default=None, max_length=16384)


class RescheduleInput(WireModel):
    item_id: StrictInt
    from_slot: StrictInt
    to_slot: StrictInt
    justification: str = Field(max_length=16384)


ACTION_PHASES = frozenset({"CHEAP_TALK", "DECISION"})
CALENDAR_TOOLS = (
    ToolSpec("get_observation", "env", EmptyInput, ACTION_PHASES, "read_only"),
    ToolSpec("schedule", "env", ScheduleInput, frozenset({"DECISION"}), "end_of_phase"),
    ToolSpec("reschedule", "env", RescheduleInput, frozenset({"DECISION", "VOLUNTARY"}), "end_of_phase", supported=False),
    ToolSpec("list_peers", "comm", EmptyInput, ACTION_PHASES, "read_only"),
    ToolSpec("send", "comm", SendInput, ACTION_PHASES, "end_of_turn"),
    ToolSpec("read_inbox", "comm", EmptyInput, ACTION_PHASES, "read_only"),
)


@dataclasses.dataclass
class CalendarRuntimeContext:
    environment: object
    io: object
    episode: object

    def close(self):
        self.environment.registry.unregister(self.episode)


class RemoteSeatClient(BaseClient):
    def __init__(self, runtime, seat_id, router):
        self.runtime = runtime
        self.seat = runtime.episode.seats[seat_id]
        self.router = router
        self.dispatcher = TurnDispatcher(runtime.io)
        self.meeting = None
        self.round_num = 0
        self.penalty = 0
        self.inbox = []
        self.pending = None
        self.lifecycle = {}
        self.completed_records = []

    def _invocation(self, kind, *, observation=None, prompt=None, phase=None, inbox=None):
        root = f"{self.runtime.io.base_url}/episodes/{self.runtime.episode.episode_id}"
        return TurnInvocation(episode_id=self.runtime.episode.episode_id, seat=self.seat.seat,
                              turn_id=uuid.uuid4().hex, kind=kind, phase=phase,
                              deadline=datetime.now(timezone.utc) + timedelta(seconds=120),
                              observation=observation or {}, prompt=prompt, inbox=inbox or [],
                              mcp={name: f"{root}/{name}/mcp" for name in ("env", "comm")})

    def register(self, agent_id, game_config):
        if agent_id != self.seat.seat:
            raise ValueError("remote seat identity mismatch")
        self.runtime.episode.wait_ready(agent_id)
        self.config = game_config
        config = dataclasses.asdict(game_config)
        invocation = self._invocation("register", observation={
            "game_config": config,
            "action_schema": {spec.name: spec.input_model.model_json_schema() for spec in CALENDAR_TOOLS},
            "action_to_tool": {"dm": "comm.send", "participant_groupchat": "comm.send",
                               "all_groupchat": "comm.send", "schedule": "env.schedule",
                               "reschedule": "env.reschedule"},
        }, prompt=build_system_prompt(config))
        self.dispatcher.dispatch(self.seat, invocation)
        with self.runtime.episode.lock:
            self.seat.registered = True

    def start_round(self, meeting, calendar_render, round_num):
        self.meeting = meeting
        self.round_num = round_num
        self.dispatcher.dispatch(self.seat, self._invocation("round_start", observation={
            "meeting": meeting, "calendar_render": calendar_render, "round": round_num,
            "incurred_penalty": self.penalty,
        }, prompt=build_round_start_message(meeting, calendar_render, round_num,
                                           incurred_penalty=self.penalty,
                                           communication_protocol=self.config.communication_protocol,
                                           communication_policy=self.config.communication_policy_by_phase.get("CHEAP_TALK"))))

    def observe_penalty(self, incurred_penalty):
        self.penalty = incurred_penalty

    def observe_messages(self, messages):
        self.inbox = messages

    def prepare(self, data, attempted_this_round):
        if data["phase"] not in ACTION_PHASES:
            raise ValueError("remote voluntary/retry turns require Phase 3")
        observation = {
            "meeting": self.meeting, "round": data["round"], "turn_index": data["turn"],
            "calendar_render": data.get("calendar_render", data.get("calendar_snapshot_render")),
            "incurred_penalty": self.penalty,
        }
        invocation = self._invocation("turn", phase=data["phase"], observation=observation,
                                      prompt=data["prompt_sent"], inbox=data.get("inbox_drained", []))
        claims = CapabilityClaims(episode_id=invocation.episode_id, attempt_id=self.runtime.episode.attempt_id,
                                  seat=self.seat.seat, turn_id=invocation.turn_id, phase=invocation.phase,
                                  allowed_tools=[f"{spec.endpoint}.{spec.name}" for spec in CALENDAR_TOOLS],
                                  exp=invocation.deadline.timestamp())
        capabilities = self.runtime.environment.registry.capabilities
        invocation.capability = capabilities.mint(claims)
        context = RoutingContext(tuple(self.config.all_agent_ids), self.meeting["id"], tuple(self.meeting["participants"]))

        def handle(spec, parsed, recorder):
            if spec.name == "get_observation":
                return ToolOutcome(status="ok", resolves="read_only", data=observation), None
            if spec.name == "read_inbox":
                return ToolOutcome(status="ok", resolves="read_only", data=invocation.inbox), None
            if spec.name == "list_peers":
                policy = self.router.topology.effective(phase=claims.phase, round=self.round_num)
                return ToolOutcome(status="ok", resolves="read_only", data={
                    "seats": list(policy.neighbors(self.seat.seat)), "channels": sorted(policy.channels),
                    "meeting_participants": list(context.meeting_participants),
                }), None
            if spec.name == "send":
                action = {"type": parsed.channel, "meeting_id": self.meeting["id"], "content": parsed.content}
                if parsed.to is not None:
                    action["to"] = parsed.to
                decision = self.router.authorize(self.seat.seat, parsed.channel, parsed.to,
                                                  phase=claims.phase, round=self.round_num, context=context,
                                                  attempted_this_round=attempted_this_round + recorder.reserved)
                if decision.charge_attempt:
                    recorder.reserved += 1
                if decision.reason is None and not decision.allowed:
                    return recorder.reject("unknown_channel", "unknown channel"), None
                # Routing denials are replayed on the worker through the same router,
                # preserving legacy charging and exactly one rejection event.
                return ToolOutcome(status="accepted" if decision.allowed else "rejected", resolves="end_of_turn",
                                   code=None if decision.allowed else "routing_denied", reason=decision.reason), action
            action = {"type": "schedule", **parsed.model_dump(exclude_none=True)}
            return ToolOutcome(status="accepted", resolves="end_of_phase"), action

        recorder = TurnRecorder(invocation, claims, capabilities, handle)
        with self.runtime.episode.lock:
            self.seat.recorder = recorder
        self.pending = invocation
        self.lifecycle = {"turn_id": invocation.turn_id, "deadline": invocation.deadline.isoformat()}
        return dict(self.lifecycle)

    def _act(self, result_type):
        if self.pending is None:
            raise RuntimeError("worker must prepare remote turn before calling client")
        try:
            completion = self.dispatcher.dispatch(self.seat, self.pending)
        finally:
            records = self.seat.recorder.close()
            self.pending = None
        self.completed_records = records
        self.lifecycle.update(closed_by="completed", attempts=1, telemetry_source="agent")
        return result_type(tool_calls=[record["action"] for record in records if "action" in record],
                           text=None, thinking=None, usage=None, latency_ms=completion.telemetry.latency_ms, raw=None)

    def worker_attempts(self):
        records, self.completed_records = self.completed_records, []
        return [record["action"] if "action" in record else {"_remote_rejection": record["rejection"]}
                for record in records]

    def turn(self, messages, turn_index=None, max_turns_per_round=None):
        if messages != self.seat.recorder.invocation.inbox:
            raise RuntimeError("pushed inbox differs from worker-drained inbox")
        return self._act(TurnResult)

    def decide(self, meeting, calendar_render):
        return self._act(DecideResult)

    def retry_decide(self, attempt, max_attempts, conflict):
        raise ValueError("remote decision retries require Phase 3")

    def voluntary_decide(self, meeting, calendar_render):
        raise ValueError("remote voluntary decisions require Phase 3")

    def reflect_calendar_belief(self, *args, **kwargs):
        raise ValueError("remote reflection requires Phase 3")

    def episode_end(self):
        if self.seat.registered and self.runtime.episode.active:
            try:
                self.dispatcher.dispatch(self.seat, self._invocation("episode_end"))
            except Exception:
                pass
