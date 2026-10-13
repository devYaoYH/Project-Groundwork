"""Calendar's executable episode graph and ordered scheduling subgraphs."""

from langgraph.graph import END, START, StateGraph
from a2a_engine.workflow_runtime import AgentStep, BatchStep, GraphProgram
from . import workflow_steps as steps


def cheap_talk_program():
    graph = StateGraph(steps.SchedulingState)
    graph.add_node("check_pass", steps.check_pass)
    graph.add_node("snapshot_active_order", steps.snapshot_active_order)
    graph.add_node("next_active", steps.check_pass)
    graph.add_node("snapshot_newly_contacted", steps.snapshot_newly_contacted)
    graph.add_node("next_contacted", steps.check_pass)
    graph.add_node("activate_contacted", AgentStep(steps.select_ordered_actor, "start_round", steps.no_observation, steps.activate_contacted))
    graph.add_node("prepare_talk", steps.prepare_talk)
    graph.add_node("ordered_talk_call", AgentStep(steps.select_ordered_actor, "turn", steps.no_observation, steps.invoke_talk))
    graph.add_node("deliver_talk", steps.deliver_talk)
    graph.add_node("advance_turn", steps.advance_turn)
    graph.add_edge(START, "check_pass")
    graph.add_conditional_edges("check_pass", steps.activity_and_cap, {"continue": "snapshot_active_order", "finished": END})
    graph.add_edge("snapshot_active_order", "next_active")
    graph.add_conditional_edges("next_active", steps.next_actor, {"invoke": "prepare_talk", "finished": "snapshot_newly_contacted"})
    graph.add_edge("snapshot_newly_contacted", "next_contacted")
    graph.add_conditional_edges("next_contacted", steps.next_actor, {"invoke": "activate_contacted", "finished": "advance_turn"})
    graph.add_edge("activate_contacted", "prepare_talk")
    graph.add_edge("prepare_talk", "ordered_talk_call")
    graph.add_edge("ordered_talk_call", "deliver_talk")
    graph.add_conditional_edges("deliver_talk", steps.talk_pass_destination, {"active": "next_active", "contacted": "next_contacted"})
    graph.add_edge("advance_turn", "check_pass")
    return GraphProgram(graph)


def decision_program(mode):
    if mode not in {"VOLUNTARY", "DECISION"}:
        raise ValueError(f"Unknown decision mode: {mode}")
    voluntary = mode == "VOLUNTARY"
    call = "voluntary_call" if voluntary else "participant_call"
    outcome = "apply_voluntary" if voluntary else "stage_participant"
    graph = StateGraph(steps.SchedulingState)
    graph.add_node("begin", steps.begin_voluntary if voluntary else steps.begin_participant)
    graph.add_node("next_ordered_actor", steps.check_pass)
    graph.add_node("prepare_decision", steps.prepare_decision)
    graph.add_node(call, AgentStep(steps.select_ordered_actor, "voluntary_decide" if voluntary else "decide", steps.no_observation, steps.invoke_decision))
    graph.add_node("record_decision_actions", steps.record_decision_actions)
    graph.add_node("validate_cell", steps.validate_decision)
    graph.add_node(outcome, steps.apply_voluntary if voluntary else steps.stage_participant)
    graph.add_node("prepare_retry", steps.prepare_retry)
    graph.add_node("retry_call", AgentStep(steps.select_ordered_actor, "retry_decide", steps.no_observation, steps.invoke_retry))
    graph.add_node("record_retry_actions", steps.record_retry_actions)
    graph.add_node("exhaust_decision", steps.exhaust_decision)
    graph.add_node("advance_actor", steps.advance_actor)
    graph.add_edge(START, "begin")
    graph.add_edge("begin", "next_ordered_actor")
    graph.add_conditional_edges("next_ordered_actor", steps.next_actor, {"invoke": "prepare_decision", "finished": END if voluntary else "transaction"})
    graph.add_edge("prepare_decision", call)
    graph.add_edge(call, "record_decision_actions")
    for name in ("record_decision_actions", "record_retry_actions"):
        graph.add_conditional_edges(name, steps.validation_enabled, {"validate": "validate_cell", "skip": "advance_actor"})
    graph.add_conditional_edges("validate_cell", steps.validation_destination, {"apply" if voluntary else "stage": outcome, "retry": "prepare_retry", "exhausted": "exhaust_decision"})
    graph.add_edge("prepare_retry", "retry_call")
    graph.add_edge("retry_call", "record_retry_actions")
    for name in (outcome, "exhaust_decision"):
        graph.add_edge(name, "advance_actor")
    graph.add_edge("advance_actor", "next_ordered_actor")
    if not voluntary:
        graph.add_node("transaction", steps.check_pass)
        graph.add_node("commit_participants", steps.commit_participants)
        graph.add_node("rollback_participants", steps.rollback_participants)
        graph.add_conditional_edges("transaction", steps.transaction_destination, {"commit": "commit_participants", "rollback": "rollback_participants"})
        graph.add_edge("commit_participants", END)
        graph.add_edge("rollback_participants", END)
    return GraphProgram(graph)


def reflection_program(launch=steps.launch_reflections, join=steps.collect_reflections):
    invocation = StateGraph(steps.SchedulingState)
    invocation.add_node("reporter_target_call", AgentStep(steps.select_reporter_target, "reflect_calendar_belief", steps.reflection_observation, steps.invoke_reflection))
    invocation.add_edge(START, "reporter_target_call")
    invocation.add_edge("reporter_target_call", END)
    graph = StateGraph(steps.SchedulingState)
    graph.add_node("launch", launch)
    graph.add_node("bounded_fan_out", BatchStep(GraphProgram(invocation), steps.reflection_calls, steps.reflection_parallelism, steps.remote_reporter_serial_key))
    graph.add_node("join_in_call_order", join)
    graph.add_edge(START, "launch")
    graph.add_edge("launch", "bounded_fan_out")
    graph.add_edge("bounded_fan_out", "join_in_call_order")
    graph.add_edge("join_in_call_order", END)
    return GraphProgram(graph)


def meeting_program():
    graph = StateGraph(steps.SchedulingState)
    for name, function in (
        ("start_meeting", steps.start_meeting),
        ("next_notification", steps.check_pass), ("advance_notification", steps.advance_actor),
        ("resolve_meeting", steps.resolve_meeting), ("fallback_search", steps.search_fallback),
        ("fallback_apply", steps.apply_fallback), ("fallback_error", steps.fallback_error),
        ("record_outcome", steps.record_outcome), ("close_meeting", steps.advance_meeting),
    ):
        graph.add_node(name, function)
    graph.add_node("notify_round", AgentStep(steps.select_ordered_actor, "start_round", steps.no_observation, steps.notify_round))
    graph.add_node("cheap_talk", cheap_talk_program().compiled)
    graph.add_node("voluntary_decisions", decision_program("VOLUNTARY").compiled)
    graph.add_node("participant_decisions", decision_program("DECISION").compiled)
    graph.add_node("round_reflection", reflection_program(steps.launch_round_reflections, steps.join_and_record_reflections).compiled)
    graph.add_edge(START, "start_meeting")
    graph.add_edge("start_meeting", "next_notification")
    graph.add_conditional_edges("next_notification", steps.next_actor, {"invoke": "notify_round", "finished": "cheap_talk"})
    graph.add_edge("notify_round", "advance_notification")
    graph.add_edge("advance_notification", "next_notification")
    graph.add_edge("cheap_talk", "voluntary_decisions")
    graph.add_edge("voluntary_decisions", "participant_decisions")
    graph.add_edge("participant_decisions", "resolve_meeting")
    graph.add_conditional_edges("resolve_meeting", steps.fallback_eligibility, {"eligible": "fallback_search", "suppressed": "record_outcome"})
    graph.add_conditional_edges("fallback_search", steps.fallback_outcome, {"success": "fallback_apply", "depth_exceeded": "fallback_error", "no_feasible_slot": "fallback_error"})
    graph.add_edge("fallback_apply", "record_outcome")
    graph.add_edge("fallback_error", "record_outcome")
    graph.add_conditional_edges("record_outcome", steps.round_reflection_enabled, {"reflect": "round_reflection", "skip": "close_meeting"})
    graph.add_edge("round_reflection", "close_meeting")
    graph.add_edge("close_meeting", END)
    return GraphProgram(graph)


def build_program():
    graph = StateGraph(steps.SchedulingState)
    graph.add_node("next_meeting", steps.check_pass)
    graph.add_node("meeting", meeting_program().compiled)
    graph.add_node("environment_reflection", reflection_program(steps.launch_environment_reflections, steps.join_and_record_reflections).compiled)
    graph.add_node("finalize", steps.finalize_episode)
    graph.add_edge(START, "next_meeting")
    graph.add_conditional_edges("next_meeting", steps.meeting_destination, {"next": "meeting", "reflect": "environment_reflection", "done": "finalize"})
    graph.add_edge("meeting", "next_meeting")
    graph.add_edge("environment_reflection", "finalize")
    graph.add_edge("finalize", END)
    return GraphProgram(graph)


def scheduling_limit(config):
    # Upper bound in supersteps: each ordered call and retry consumes several nodes.
    return max(100, 30 + 12 * max(0, config.max_turns_per_round) * config.num_agents
               + 14 * config.num_agents * (max(0, config.decision_retries) + 1))


def episode_limit(config, meetings):
    # Upper bound for the meeting loop and its nested scheduling/reflection graphs.
    return 100 + max(0, meetings) * (scheduling_limit(config) + 10 * config.num_agents + 50)
