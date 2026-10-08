"""Remote BaseClient; the calendar worker remains authoritative."""

import dataclasses
import uuid
import time
import threading
import math
from copy import deepcopy
from datetime import datetime, timedelta, timezone

from pydantic import Field, StrictInt

from a2a_engine.comm import RoutingContext
from a2a_engine.remote.contract import CapabilityClaims, ToolOutcome, TurnInvocation, WireModel
from a2a_engine.remote.dispatch import DeliveryFailure, TurnDispatcher
from a2a_engine.remote.turns import ToolSpec, TurnRecorder
from calendar_game.agents import BaseClient, DecideResult, ReflectionResult, TokenUsage, TurnResult
from calendar_game.prompts import build_reflection_message, build_round_start_message, build_system_prompt
from calendar_game.privacy import hydrate_calendar_render_for_llm, hydrate_meeting_for_llm


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


ACTION_PHASES = frozenset({"CHEAP_TALK", "DECISION", "VOLUNTARY", "DECISION_RETRY"})
CALENDAR_TOOLS = (
    ToolSpec("get_observation", "env", EmptyInput, ACTION_PHASES, "read_only"),
    ToolSpec("schedule", "env", ScheduleInput, frozenset({"DECISION", "DECISION_RETRY"}), "end_of_phase"),
    ToolSpec("reschedule", "env", RescheduleInput, frozenset({"DECISION", "VOLUNTARY", "DECISION_RETRY"}), "end_of_phase"),
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
    def __init__(self, runtime, seat_id, router, *, turn_timeout_s=120, clock=time.monotonic,
                 private_prompts=False):
        self.runtime = runtime
        self.seat = runtime.episode.seats[seat_id]
        self.router = router
        self.clock = clock
        self.turn_timeout_s = turn_timeout_s
        self.private_prompts = private_prompts
        self.dispatcher = TurnDispatcher(runtime.io, clock=clock)
        self.meeting = None
        self.round_num = 0
        self.penalty = 0
        self.inbox = []
        self.pending = None
        self.lifecycle = {}
        self.completed_records = []
        self.reflection_lock = threading.Lock()

    def _timeout(self, phase):
        if isinstance(self.turn_timeout_s, dict):
            return self.turn_timeout_s.get(phase, 120)
        return self.turn_timeout_s

    def _invocation(self, kind, *, observation=None, prompt=None, phase=None, inbox=None):
        root = f"{self.runtime.io.base_url}/episodes/{self.runtime.episode.episode_id}"
        return TurnInvocation(episode_id=self.runtime.episode.episode_id, seat=self.seat.seat,
                              turn_id=uuid.uuid4().hex, kind=kind, phase=phase,
                              deadline=datetime.now(timezone.utc) + timedelta(seconds=self._timeout(phase or kind)),
                              observation=observation or {}, prompt=prompt, inbox=inbox or [],
                              mcp={name: f"{root}/{name}/mcp" for name in ("env", "comm")})

    def register(self, agent_id, game_config):
        if agent_id != self.seat.seat:
            raise ValueError("remote seat identity mismatch")
        self.runtime.episode.wait_ready(agent_id)
        self.config = game_config
        config = dataclasses.asdict(game_config)
        deadline = self.clock() + self._timeout("register")
        invocation = self._invocation("register", observation={
            "game_config": config,
            "action_schema": {spec.name: spec.input_model.model_json_schema() for spec in CALENDAR_TOOLS},
            "action_to_tool": {"dm": "comm.send", "participant_groupchat": "comm.send",
                               "all_groupchat": "comm.send", "schedule": "env.schedule",
                               "reschedule": "env.reschedule"},
        }, prompt=build_system_prompt(config))
        try:
            self.dispatcher.dispatch(self.seat, invocation, deadline=deadline)
        except DeliveryFailure:
            raise TimeoutError("seat_unavailable") from None
        with self.runtime.episode.lock:
            self.seat.registered = True

    def start_round(self, meeting, calendar_render, round_num):
        self.meeting = deepcopy(meeting)
        self.round_num = round_num
        prompt_meeting, prompt_calendar = meeting, calendar_render
        if self.private_prompts:
            key = f"agent:{self.seat.seat}:round:{round_num}"
            prompt_meeting = hydrate_meeting_for_llm(meeting, stable_key=key)
            prompt_calendar = hydrate_calendar_render_for_llm(calendar_render, stable_key=key)
        deadline = self.clock() + self._timeout("round_start")
        invocation = self._invocation("round_start", observation={
            "meeting": meeting, "calendar_render": calendar_render, "round": round_num,
            "incurred_penalty": self.penalty,
        }, prompt=build_round_start_message(prompt_meeting, prompt_calendar, round_num,
                                           incurred_penalty=self.penalty,
                                           communication_protocol=self.config.communication_protocol,
                                           communication_policy=self.config.communication_policy_by_phase.get("CHEAP_TALK")))
        try:
            self.dispatcher.dispatch(self.seat, invocation, deadline=deadline)
        except DeliveryFailure:
            pass

    def observe_penalty(self, incurred_penalty):
        self.penalty = incurred_penalty

    def observe_messages(self, messages):
        self.inbox = messages

    def prepare(self, data, attempted_this_round):
        deadline = self.clock() + self._timeout(data["phase"])
        parent_phase = data.get("parent_phase", data["phase"])
        if data["phase"] == "DECISION_RETRY" and parent_phase not in {"DECISION", "VOLUNTARY"}:
            raise ValueError("retry requires a decision or voluntary parent phase")
        observation = {
            "meeting": deepcopy(self.meeting), "round": data["round"], "turn_index": data["turn"],
            "calendar_render": data.get("calendar_render", data.get("calendar_snapshot_render")),
            "incurred_penalty": self.penalty,
        }
        invocation = self._invocation("turn", phase=data["phase"], observation=observation,
                                      prompt=data["prompt_sent"], inbox=data.get("inbox_drained", []))
        if data["phase"] == "DECISION_RETRY":
            invocation.parent_phase = parent_phase
            invocation.observation.update(attempt=data["attempt"], max_attempts=data["max_attempts"], conflict=data["conflict"])
        claims = CapabilityClaims(episode_id=invocation.episode_id, attempt_id=self.runtime.episode.attempt_id,
                                  seat=self.seat.seat, turn_id=invocation.turn_id, phase=invocation.phase,
                                  allowed_tools=[f"{spec.endpoint}.{spec.name}" for spec in CALENDAR_TOOLS],
                                  exp=invocation.deadline.timestamp())
        capabilities = self.runtime.environment.registry.capabilities
        invocation.capability = capabilities.mint(claims)
        context = RoutingContext(tuple(self.config.all_agent_ids), self.meeting["id"], tuple(self.meeting["participants"]))

        def handle(spec, parsed, recorder):
            if spec.name == "get_observation":
                return ToolOutcome(status="ok", resolves="read_only", data=invocation.observation), None
            if spec.name == "read_inbox":
                return ToolOutcome(status="ok", resolves="read_only", data=invocation.inbox), None
            if spec.name == "list_peers":
                policy = self.router.topology.effective(phase=parent_phase, round=self.round_num)
                return ToolOutcome(status="ok", resolves="read_only", data={
                    "seats": list(policy.neighbors(self.seat.seat)), "channels": sorted(policy.channels),
                    "meeting_participants": list(context.meeting_participants),
                }), None
            if spec.name == "send":
                action = {"type": parsed.channel, "meeting_id": self.meeting["id"], "content": parsed.content}
                if parsed.to is not None:
                    action["to"] = parsed.to
                decision = self.router.authorize(self.seat.seat, parsed.channel, parsed.to,
                                                  phase=parent_phase, round=self.round_num, context=context,
                                                  attempted_this_round=attempted_this_round + recorder.reserved)
                if decision.charge_attempt:
                    recorder.reserved += 1
                if decision.reason is None and not decision.allowed:
                    return recorder.reject("unknown_channel", "unknown channel"), None
                # Routing denials are replayed on the worker through the same router,
                # preserving legacy charging and exactly one rejection event.
                return ToolOutcome(status="accepted" if decision.allowed else "rejected", resolves="end_of_turn",
                                   code=None if decision.allowed else "routing_denied", reason=decision.reason), action
            if spec.name == "schedule" and parent_phase != "DECISION":
                return recorder.reject("wrong_phase", "schedule requires a participant decision turn"), None
            action = {"type": spec.name, **parsed.model_dump(exclude_none=True)}
            timing = "end_of_turn" if parent_phase == "VOLUNTARY" else "end_of_phase"
            return ToolOutcome(status="accepted", resolves=timing), action

        recorder = TurnRecorder(invocation, claims, capabilities, handle, clock=self.clock,
                                deadline=deadline)
        with self.runtime.episode.lock:
            self.seat.recorder = recorder
        self.pending = invocation
        self.lifecycle = {"turn_id": invocation.turn_id, "deadline": invocation.deadline.isoformat()}
        if invocation.parent_phase:
            self.lifecycle.update(parent_phase=parent_phase, attempt=data["attempt"])
        return dict(self.lifecycle)

    def _act(self, result_type):
        if self.pending is None:
            raise RuntimeError("worker must prepare remote turn before calling client")
        try:
            completion = self.dispatcher.dispatch(self.seat, self.pending, self.seat.recorder)
        finally:
            records = self.seat.recorder.close()
            self.pending = None
        self.completed_records = records
        recorder = self.seat.recorder
        self.lifecycle.update(closed_by=recorder.closed_by, attempts=recorder.attempts,
                              telemetry_source="agent" if completion else "environment")
        return result_type(tool_calls=[record["action"] for record in records if "action" in record],
                           text=None, thinking=None,
                           usage=TokenUsage(**completion.telemetry.usage.model_dump()) if completion and completion.telemetry.usage else None,
                           latency_ms=completion.telemetry.latency_ms if completion else None, raw=None)

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
        if self.pending.phase != "DECISION_RETRY":
            raise RuntimeError("worker must prepare retry turn")
        result = self._act(DecideResult)
        result.retry_count = attempt
        return result

    def voluntary_decide(self, meeting, calendar_render):
        return self._act(DecideResult)

    def reflect_calendar_belief(self, target_agent_id, num_slots, round_num=None):
        deadline = self.clock() + self._timeout("REFLECTION")
        invocation = self._invocation("reflect", phase="REFLECTION", observation={
            "target_agent_id": target_agent_id, "num_slots": num_slots, "round": round_num,
        }, prompt=build_reflection_message(target_agent_id, num_slots, round_num))
        if not self.reflection_lock.acquire(timeout=max(0, deadline - self.clock())):
            return None
        try:
            try:
                completion = self.dispatcher.dispatch(self.seat, invocation, deadline=deadline)
            except DeliveryFailure:
                return None
            if completion.reflection is None:
                return None
            estimates = completion.reflection.get("estimates")
            if not isinstance(estimates, list) or len(estimates) != num_slots:
                return None
            clean = []
            for slot, estimate in enumerate(estimates):
                if not isinstance(estimate, dict):
                    return None
                fields = {key: estimate.get(key) for key in (
                    "belief_delta_occupied", "estimate_state", "probability_free", "probability_busy", "confidence")}
                for key, value in fields.items():
                    if value is None:
                        continue
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                        return None
                    if key == "belief_delta_occupied" and (not isinstance(value, int) or not -3 <= value <= 3):
                        return None
                    if key == "estimate_state" and value not in {0, 1}:
                        return None
                    if key in {"probability_free", "probability_busy", "confidence"} and not 0 <= value <= 1:
                        return None
                logprobs = estimate.get("logprobs", {})
                if not isinstance(logprobs, dict):
                    return None
                logprobs = {key: logprobs.get(key) for key in ("0", "1", "_top_logprob_floor")}
                if any(value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                              or not math.isfinite(value) or value > 0) for value in logprobs.values()):
                    return None
                clean.append({"target_agent_id": target_agent_id, "slot": slot, **fields, "logprobs": logprobs})
            usage = TokenUsage(**completion.telemetry.usage.model_dump()) if completion.telemetry.usage else None
            return ReflectionResult(target_agent_id, clean, None, usage, completion.telemetry.latency_ms, None)
        finally:
            self.reflection_lock.release()

    def episode_end(self):
        if self.seat.registered and self.runtime.episode.active:
            try:
                deadline = self.clock() + self._timeout("episode_end")
                self.dispatcher.dispatch(self.seat, self._invocation("episode_end"), deadline=deadline)
            except Exception:
                pass
