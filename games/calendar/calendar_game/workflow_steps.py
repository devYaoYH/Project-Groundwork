"""Calendar-owned scheduling state and domain leaves for executable boundaries."""

import asyncio
from dataclasses import dataclass, field
from typing import Callable, TypedDict

from langgraph.runtime import Runtime

from a2a_engine.comm import CHANNELS
from a2a_engine.workflow_runtime import require_agent_step
from .calendar import apply_cell, validate_cell
from .prompts import (
    append_phase_inbox, build_cheap_talk_prompt, build_decision_message,
    build_reflection_message, build_retry_message, build_voluntary_reschedule_message,
)


class SchedulingState(TypedDict, total=False):
    boundary: str
    turn_index: int
    has_activity: bool
    active_agent_ids: set[int]
    active_agent_order: list[int]
    already_queued: set[int]
    round_turn_agent_ids: set[int]
    actor_order: list[int]
    cursor: int
    newly_contacted: bool
    mode: str
    protocol: str
    attempt: int
    result: object
    actions: list[dict]
    valid: bool
    conflict: str
    blocked_slot_violations: list[dict]
    staged_decisions: dict
    decision_phase_failed: bool
    calls: list[dict]
    call: dict
    batch_results: list
    reflection_estimates: list[dict]
    reflection_call_counts: dict[int, int]


@dataclass
class RoundContext:
    game: object
    agents: list
    meeting: dict
    round_num: int
    speaker_order: list[int]
    communication_protocol: str
    communication_policy_by_phase: dict
    displacement_cost: dict
    total_client_calls: dict
    deliver: Callable
    enter_phase: Callable


@dataclass
class ReflectionContext:
    game: object
    agents: list
    all_agent_ids: list[int]
    round_num: int | None
    event_round: int
    reporter_agent_ids: list[int] | None = None
    prepared: dict = field(default_factory=dict)


def select_ordered_actor(state, context):
    return context.agents[state["actor_order"][state["cursor"]]]


def no_observation(state, context):
    return None


def event_data(state, context, phase, **extra):
    return {"round": context.round_num, "turn": state["turn_index"], "phase": phase,
            "agent_id": state["actor_order"][state["cursor"]], **extra}


def result_data(result):
    return {"tool_calls": result.tool_calls, "text": result.text, "thinking": result.thinking,
            "usage": result.usage.__dict__ if result.usage else None,
            "latency_ms": result.latency_ms, "raw_api_response": result.raw}


async def check_pass(state):
    return {}


def activity_and_cap(state, runtime: Runtime):
    return "continue" if state["has_activity"] and state["turn_index"] < runtime.context.game.config.max_turns_per_round else "finished"


async def snapshot_active_order(state):
    return {"actor_order": list(state["active_agent_order"]), "cursor": 0,
            "newly_contacted": False, "has_activity": False}


async def snapshot_newly_contacted(state):
    return {"actor_order": sorted(state["already_queued"] - state["active_agent_ids"]),
            "cursor": 0, "newly_contacted": True}


def next_actor(state):
    return "invoke" if state["cursor"] < len(state["actor_order"]) else "finished"


async def activate_contacted(actor, operation, observation, state, context):
    require_agent_step()
    actor.start_round(context.meeting, context.round_num, incurred_penalty=context.displacement_cost[actor.agent_id])
    return {"active_agent_ids": state["active_agent_ids"] | {actor.agent_id},
            "active_agent_order": [*state["active_agent_order"], actor.agent_id]}


async def prepare_talk(state, runtime: Runtime):
    ctx = runtime.context
    agent = select_ordered_actor(state, ctx)
    inbox = list(agent.inbox_queue)
    calendar = ctx.game._prompt_calendar_for_agent(agent, agent.calendar.render(), ctx.round_num)
    prompt = build_cheap_talk_prompt(
        ctx.game._prompt_meeting_for_agent(agent, ctx.meeting, ctx.round_num), calendar, ctx.round_num, inbox,
        first_turn=state["newly_contacted"] or agent.agent_id not in state["round_turn_agent_ids"],
        incurred_penalty=ctx.displacement_cost[agent.agent_id], turn_index=state["turn_index"],
        max_turns_per_round=ctx.game.config.max_turns_per_round,
        communication_protocol=ctx.communication_protocol,
        communication_policy=ctx.communication_policy_by_phase.get("CHEAP_TALK"),
    )
    ctx.game._append_event("turn_start", data=event_data(state, ctx, "CHEAP_TALK",
                           inbox_drained=inbox, calendar_render=calendar, prompt_sent=prompt))
    return {}


async def invoke_talk(actor, operation, observation, state, context):
    require_agent_step()
    return {"result": actor.turn(state["turn_index"], context.game.config.max_turns_per_round)}


async def record_talk(state, runtime: Runtime):
    ctx = runtime.context
    agent_id = state["actor_order"][state["cursor"]]
    ctx.total_client_calls[agent_id] += 1
    ctx.game._append_event("turn_end", data=event_data(state, ctx, "CHEAP_TALK", **result_data(state["result"])))
    return {"round_turn_agent_ids": state["round_turn_agent_ids"] | {agent_id}}


async def process_delivery(state, runtime: Runtime):
    ctx = runtime.context
    agent_id = state["actor_order"][state["cursor"]]
    queued = set(state["already_queued"])
    active = state["has_activity"]
    for tool in ctx.game._worker_attempts(agent_id, state["result"].tool_calls):
        if not isinstance(tool, dict):
            ctx.game._invalid_tool_call(round_num=ctx.round_num, turn=state["turn_index"], phase="CHEAP_TALK",
                                        agent_id=agent_id, tool=tool, reason="tool call is not an object")
        elif ctx.deliver(tool=tool, agent_id=agent_id, meeting=ctx.meeting, round_num=ctx.round_num,
                         turn_index=state["turn_index"], already_queued=queued):
            active = True
    return {"already_queued": queued, "has_activity": active, "cursor": state["cursor"] + 1}


def talk_pass_destination(state):
    return "contacted" if state["newly_contacted"] else "active"


async def advance_turn(state):
    return {"turn_index": state["turn_index"] + 1}


async def initialize_decisions(state, runtime: Runtime):
    ctx = runtime.context
    mode = state["mode"]
    return {"protocol": ctx.enter_phase(mode, ctx.round_num), "cursor": 0, "attempt": 0,
            "actor_order": sorted(state["already_queued"] - set(ctx.meeting["participants"])) if mode == "VOLUNTARY" else ctx.speaker_order,
            "staged_decisions": {}, "decision_phase_failed": False}


def decision_inbox(agent, context):
    if context.game.topology.legacy:
        return {}
    messages = list(agent.inbox_queue)
    agent.inbox_queue.clear()
    agent.client.observe_messages(messages)
    return {"inbox_drained": messages}


async def prepare_decision(state, runtime: Runtime):
    ctx = runtime.context
    agent = select_ordered_actor(state, ctx)
    inbox = decision_inbox(agent, ctx)
    voluntary = state["mode"] == "VOLUNTARY"
    calendar = agent.calendar.render() if voluntary else agent.calendar.snapshot().render()
    calendar = ctx.game._prompt_calendar_for_agent(agent, calendar, ctx.round_num)
    meeting = ctx.game._prompt_meeting_for_agent(agent, ctx.meeting, ctx.round_num)
    prompt = (build_voluntary_reschedule_message if voluntary else build_decision_message)(
        meeting, calendar, state["protocol"], communication_policy=ctx.communication_policy_by_phase.get(state["mode"]),
    )
    ctx.game._append_event("decide_start", data=event_data(state, ctx, state["mode"],
        **{"calendar_render" if voluntary else "calendar_snapshot_render": calendar},
        prompt_sent=append_phase_inbox(prompt, inbox.get("inbox_drained", [])), **inbox))
    return {"attempt": 0}


def decision_kind(state):
    return state["mode"]


async def invoke_decision(actor, operation, observation, state, context):
    require_agent_step()
    result = actor.voluntary_decide(context.meeting) if operation == "voluntary_decide" else actor.decide(context.meeting)
    return {"result": result}


async def record_decision(state, runtime: Runtime):
    ctx = runtime.context
    ctx.total_client_calls[state["actor_order"][state["cursor"]]] += 1
    extra = {} if state["mode"] == "VOLUNTARY" else {"retry_count": state["result"].retry_count, "status": "pending"}
    ctx.game._append_event("decide_end", data=event_data(state, ctx, state["mode"], **result_data(state["result"]), **extra))
    return {}


async def filter_actions(state, runtime: Runtime):
    ctx = runtime.context
    agent_id = state["actor_order"][state["cursor"]]
    calls = ctx.game._worker_attempts(agent_id, state["result"].tool_calls)
    queued = set(state["already_queued"])
    if not ctx.game.topology.legacy or agent_id in ctx.game._remote_clients:
        actions = []
        for tool in calls:
            if ctx.game._remote_rejection(tool, agent_id=agent_id, round_num=ctx.round_num,
                                          turn=state["turn_index"], phase=state["mode"]):
                continue
            if isinstance(tool, dict) and ctx.game._canonical_tool_type(tool.get("type")) in CHANNELS:
                ctx.deliver(tool=tool, agent_id=agent_id, meeting=ctx.meeting, round_num=ctx.round_num,
                            turn_index=state["turn_index"], already_queued=queued, phase=state["mode"])
            else:
                actions.append(tool)
        calls = actions
    if state["mode"] == "VOLUNTARY":
        actions = [a for a in calls if isinstance(a, dict) and a.get("type") == "reschedule"]
    else:
        actions = ctx.game._decision_actions(calls, ctx.meeting, round_num=ctx.round_num,
            turn_index=state["turn_index"], agent_id=agent_id, attempt=state["attempt"])
    return {"actions": actions, "already_queued": queued}


async def validate_decision(state, runtime: Runtime):
    ctx = runtime.context
    agent = select_ordered_actor(state, ctx)
    violations = ctx.game._blocked_reschedule_violations(agent.calendar, state["actions"],
        agent_id=agent.agent_id, phase=state["mode"], attempt=state["attempt"])
    ok, conflict = validate_cell(agent.calendar, state["actions"], require_schedule=state["mode"] == "DECISION")
    if not ok:
        ctx.game._append_event("cell_rejected", data=event_data(state, ctx, state["mode"],
            attempt=state["attempt"], conflict_description=conflict, actions=state["actions"]))
    return {"valid": ok, "conflict": conflict, "blocked_slot_violations": [*state["blocked_slot_violations"], *violations]}


def validation_destination(state, runtime: Runtime):
    if state["valid"]:
        return "apply" if state["mode"] == "VOLUNTARY" else "stage"
    return "retry" if state["attempt"] < runtime.context.game.config.decision_retries else "exhausted"


def validation_enabled(state, runtime: Runtime):
    return "validate" if runtime.context.game.config.decision_retries >= 0 else "skip"


def displacement(calendar, actions):
    return sum(int(item["cost"]) for action in actions if action.get("type") == "reschedule"
               for item in [calendar.get(int(action["from_slot"]))] if isinstance(item, dict) and "cost" in item)


async def apply_voluntary(state, runtime: Runtime):
    ctx = runtime.context
    agent = select_ordered_actor(state, ctx)
    ctx.displacement_cost[agent.agent_id] += displacement(agent.calendar, state["actions"])
    apply_cell(agent.calendar, state["actions"])
    ctx.game._append_event("cell_applied", data=event_data(state, ctx, "VOLUNTARY",
        actions=state["actions"], calendar_render_after=agent.calendar.render()))
    return {}


async def stage_participant(state, runtime: Runtime):
    ctx = runtime.context
    agent = select_ordered_actor(state, ctx)
    calendar = agent.calendar.snapshot()
    cost = displacement(agent.calendar, state["actions"])
    apply_cell(calendar, state["actions"])
    return {"staged_decisions": {**state["staged_decisions"], agent.agent_id: (calendar, state["actions"], cost)}}


async def prepare_retry(state, runtime: Runtime):
    ctx = runtime.context
    agent = select_ordered_actor(state, ctx)
    attempt = state["attempt"] + 1
    inbox = decision_inbox(agent, ctx)
    if agent.agent_id in ctx.game._remote_clients:
        ctx.game._append_event("decide_start", data=event_data(state, ctx, "DECISION_RETRY",
            parent_phase=state["mode"], attempt=attempt, max_attempts=ctx.game.config.decision_retries,
            conflict=state["conflict"],
            calendar_snapshot_render=ctx.game._prompt_calendar_for_agent(agent, agent.calendar.snapshot().render(), ctx.round_num),
            prompt_sent=append_phase_inbox(build_retry_message(attempt, ctx.game.config.decision_retries,
                state["conflict"], parent_phase=state["mode"]), inbox.get("inbox_drained", [])), **inbox))
    return {"attempt": attempt}


async def invoke_retry(actor, operation, observation, state, context):
    require_agent_step()
    return {"result": actor.client.retry_decide(state["attempt"], context.game.config.decision_retries, state["conflict"])}


async def record_retry(state, runtime: Runtime):
    ctx = runtime.context
    agent = select_ordered_actor(state, ctx)
    ctx.total_client_calls[agent.agent_id] += 1
    if agent.agent_id in ctx.game._remote_clients:
        ctx.game._append_event("decide_end", data=event_data(state, ctx, "DECISION_RETRY",
            parent_phase=state["mode"], attempt=state["attempt"], **result_data(state["result"]),
            retry_count=state["result"].retry_count, status="pending"))
    return {}


async def exhaust_decision(state, runtime: Runtime):
    if state["mode"] == "DECISION":
        ctx = runtime.context
        ctx.game._append_event("decision_failed", data=event_data(state, ctx, "DECISION",
                              attempts_exhausted=ctx.game.config.decision_retries + 1))
        return {"decision_phase_failed": True}
    return {}


async def advance_actor(state):
    return {"cursor": state["cursor"] + 1}


def transaction_destination(state, runtime: Runtime):
    if state["mode"] == "VOLUNTARY":
        return "voluntary_done"
    return "commit" if (not state["decision_phase_failed"] and not state["blocked_slot_violations"]
                        and len(state["staged_decisions"]) == len(runtime.context.speaker_order)) else "rollback"


async def commit_participants(state, runtime: Runtime):
    ctx = runtime.context
    for agent_id in ctx.speaker_order:
        calendar, actions, cost = state["staged_decisions"][agent_id]
        ctx.agents[agent_id].calendar.slots = calendar.slots
        ctx.agents[agent_id].calendar.meeting_participants = calendar.meeting_participants
        ctx.displacement_cost[agent_id] += cost
        ctx.game._append_event("cell_applied", data={"round": ctx.round_num, "turn": state["turn_index"],
            "phase": "DECISION", "agent_id": agent_id, "actions": actions,
            "calendar_render_after": ctx.agents[agent_id].calendar.render()})
    return {}


async def rollback_participants(state, runtime: Runtime):
    ctx = runtime.context
    reason = "blocked slot violation" if state["blocked_slot_violations"] else "another participant failed decision validation"
    for agent_id, (_calendar, actions, _cost) in state["staged_decisions"].items():
        ctx.game._append_event("cell_rolled_back", data={"round": ctx.round_num, "turn": state["turn_index"],
            "phase": "DECISION", "agent_id": agent_id, "actions": actions, "reason": reason,
            "calendar_render_after": ctx.agents[agent_id].calendar.render()})
    return {}


async def launch_reflections(state, runtime: Runtime):
    ctx = runtime.context
    calls = []
    reporters = ctx.all_agent_ids if ctx.reporter_agent_ids is None else ctx.reporter_agent_ids
    for agent_id in reporters:
        for target in ctx.all_agent_ids:
            if target == agent_id:
                continue
            ctx.game._append_event("reflection_start", data={"round": ctx.event_round, "turn": 0,
                "phase": "REFLECTION", "agent_id": agent_id, "target_agent_id": target,
                "num_slots": ctx.game.config.num_slots, "reflection_scope": "environment" if ctx.round_num is None else "round",
                "prompt_sent": build_reflection_message(target, ctx.game.config.num_slots, round_num=ctx.round_num)})
            index = len(calls)
            remote = ctx.game._remote_clients.get(agent_id)
            if remote is not None:
                ctx.prepared[index] = remote.prepare_reflection(target, ctx.game.config.num_slots, ctx.round_num)
            calls.append({"agent_id": agent_id, "target_agent_id": target, "index": index})
    return {"calls": calls}


def reflection_calls(state, context):
    return state["calls"]


def reflection_parallelism(state, context):
    value = context.game.config.reflection_max_parallelism
    return max(1, int(value)) if value is not None and int(value) > 0 else None


def remote_reporter_serial_key(call, context):
    return call["agent_id"] if call["agent_id"] in context.game._remote_clients else None


def select_reporter_target(state, context):
    return context.agents[state["call"]["agent_id"]]


def reflection_observation(state, context):
    return {"target_agent_id": state["call"]["target_agent_id"], "num_slots": context.game.config.num_slots,
            "round_num": context.round_num}


async def invoke_reflection(actor, operation, observation, state, context):
    require_agent_step()
    prepared = context.prepared.get(state["call"]["index"])
    if prepared is not None:
        observation = {**observation, "prepared": prepared}
    result = await asyncio.to_thread(actor.client.reflect_calendar_belief, **observation)
    return {"result": result}


async def join_reflections(state):
    return {}


async def collect_reflections(state, runtime: Runtime):
    ctx = runtime.context
    estimates, counts = [], {}
    for call, output in zip(state["calls"], state["batch_results"], strict=True):
        agent_id, target = call["agent_id"], call["target_agent_id"]
        result = None if isinstance(output, BaseException) else output["result"]
        scope = "environment" if ctx.round_num is None else "round"
        if result is not None:
            counts[agent_id] = counts.get(agent_id, 0) + 1
            rows = [{"agent_id": agent_id, "round": ctx.event_round, "reflection_scope": scope, **row} for row in result.estimates]
            estimates.extend(rows)
            data = {"estimates": rows, "text": result.text, "usage": result.usage.__dict__ if result.usage else None,
                    "latency_ms": result.latency_ms, "raw_api_response": result.raw}
        else:
            rows = [{"agent_id": agent_id, "target_agent_id": target, "round": ctx.event_round,
                     "reflection_scope": scope, "slot": slot, "estimate_state": None,
                     "probability_free": None, "probability_busy": None, "confidence": None,
                     "logprobs": {"0": None, "1": None}} for slot in range(ctx.game.config.num_slots)]
            data = {"estimates": rows, "text": None, "usage": None, "latency_ms": None, "raw_api_response": None}
        ctx.game._append_event("reflection_end", data={"round": ctx.event_round, "turn": 0, "phase": "REFLECTION",
            "agent_id": agent_id, "target_agent_id": target, "num_slots": ctx.game.config.num_slots,
            "reflection_scope": scope, **data})
    return {"reflection_estimates": estimates, "reflection_call_counts": counts}
