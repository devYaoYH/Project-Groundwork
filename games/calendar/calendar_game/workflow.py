"""Executable Calendar boundaries called by the transitional game driver."""

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
    graph.add_node("record_talk", steps.record_talk)
    graph.add_node("process_delivery", steps.process_delivery)
    graph.add_node("advance_turn", steps.advance_turn)
    graph.add_edge(START, "check_pass")
    graph.add_conditional_edges("check_pass", steps.activity_and_cap, {"continue": "snapshot_active_order", "finished": END})
    graph.add_edge("snapshot_active_order", "next_active")
    graph.add_conditional_edges("next_active", steps.next_actor, {"invoke": "prepare_talk", "finished": "snapshot_newly_contacted"})
    graph.add_edge("snapshot_newly_contacted", "next_contacted")
    graph.add_conditional_edges("next_contacted", steps.next_actor, {"invoke": "activate_contacted", "finished": "advance_turn"})
    graph.add_edge("activate_contacted", "prepare_talk")
    graph.add_edge("prepare_talk", "ordered_talk_call")
    graph.add_edge("ordered_talk_call", "record_talk")
    graph.add_edge("record_talk", "process_delivery")
    graph.add_conditional_edges("process_delivery", steps.talk_pass_destination, {"active": "next_active", "contacted": "next_contacted"})
    graph.add_edge("advance_turn", "check_pass")
    return GraphProgram(graph)


def decision_program():
    graph = StateGraph(steps.SchedulingState)
    graph.add_node("initialize_decisions", steps.initialize_decisions)
    graph.add_node("next_ordered_actor", steps.check_pass)
    graph.add_node("prepare_decision", steps.prepare_decision)
    graph.add_node("voluntary_call", AgentStep(steps.select_ordered_actor, "voluntary_decide", steps.no_observation, steps.invoke_decision))
    graph.add_node("participant_call", AgentStep(steps.select_ordered_actor, "decide", steps.no_observation, steps.invoke_decision))
    graph.add_node("record_decision", steps.record_decision)
    graph.add_node("filter_actions", steps.filter_actions)
    graph.add_node("validate_cell", steps.validate_decision)
    graph.add_node("apply_voluntary", steps.apply_voluntary)
    graph.add_node("stage_participant", steps.stage_participant)
    graph.add_node("prepare_retry", steps.prepare_retry)
    graph.add_node("retry_call", AgentStep(steps.select_ordered_actor, "retry_decide", steps.no_observation, steps.invoke_retry))
    graph.add_node("record_retry", steps.record_retry)
    graph.add_node("exhaust_decision", steps.exhaust_decision)
    graph.add_node("advance_actor", steps.advance_actor)
    graph.add_node("transaction", steps.check_pass)
    graph.add_node("commit_participants", steps.commit_participants)
    graph.add_node("rollback_participants", steps.rollback_participants)
    graph.add_edge(START, "initialize_decisions")
    graph.add_edge("initialize_decisions", "next_ordered_actor")
    graph.add_conditional_edges("next_ordered_actor", steps.next_actor, {"invoke": "prepare_decision", "finished": "transaction"})
    graph.add_conditional_edges("prepare_decision", steps.decision_kind, {"VOLUNTARY": "voluntary_call", "DECISION": "participant_call"})
    graph.add_edge("voluntary_call", "record_decision")
    graph.add_edge("participant_call", "record_decision")
    graph.add_edge("record_decision", "filter_actions")
    graph.add_conditional_edges("filter_actions", steps.validation_enabled, {"validate": "validate_cell", "skip": "advance_actor"})
    graph.add_conditional_edges("validate_cell", steps.validation_destination, {"apply": "apply_voluntary", "stage": "stage_participant", "retry": "prepare_retry", "exhausted": "exhaust_decision"})
    graph.add_edge("prepare_retry", "retry_call")
    graph.add_edge("retry_call", "record_retry")
    graph.add_edge("record_retry", "filter_actions")
    for name in ("apply_voluntary", "stage_participant", "exhaust_decision"):
        graph.add_edge(name, "advance_actor")
    graph.add_edge("advance_actor", "next_ordered_actor")
    graph.add_conditional_edges("transaction", steps.transaction_destination, {"voluntary_done": END, "commit": "commit_participants", "rollback": "rollback_participants"})
    graph.add_edge("commit_participants", END)
    graph.add_edge("rollback_participants", END)
    return GraphProgram(graph)


def reflection_program():
    invocation = StateGraph(steps.SchedulingState)
    invocation.add_node("reporter_target_call", AgentStep(steps.select_reporter_target, "reflect_calendar_belief", steps.reflection_observation, steps.invoke_reflection))
    invocation.add_edge(START, "reporter_target_call")
    invocation.add_edge("reporter_target_call", END)
    graph = StateGraph(steps.SchedulingState)
    graph.add_node("launch_reflections", steps.launch_reflections)
    graph.add_node("bounded_fan_out", BatchStep(GraphProgram(invocation), steps.reflection_calls, steps.reflection_parallelism, steps.remote_reporter_serial_key))
    graph.add_node("join_reflections", steps.join_reflections)
    graph.add_node("collect_in_call_order", steps.collect_reflections)
    graph.add_edge(START, "launch_reflections")
    graph.add_edge("launch_reflections", "bounded_fan_out")
    graph.add_edge("bounded_fan_out", "join_reflections")
    graph.add_edge("join_reflections", "collect_in_call_order")
    graph.add_edge("collect_in_call_order", END)
    return GraphProgram(graph)


def select_driver_boundary(state):
    return state["boundary"]


def build_program():
    graph = StateGraph(steps.SchedulingState)
    graph.add_node("driver_boundary", steps.check_pass)
    graph.add_node("cheap_talk_pass", cheap_talk_program().compiled)
    graph.add_node("decision_batch", decision_program().compiled)
    graph.add_node("reflection_batch", reflection_program().compiled)
    graph.add_edge(START, "driver_boundary")
    graph.add_conditional_edges("driver_boundary", select_driver_boundary, {
        "cheap_talk": "cheap_talk_pass", "decision": "decision_batch", "reflection": "reflection_batch",
    })
    for name in ("cheap_talk_pass", "decision_batch", "reflection_batch"):
        graph.add_edge(name, END)
    return GraphProgram(graph, incomplete_reason="calendar_driver_not_migrated")


def scheduling_limit(config):
    # Supersteps, not turns: each ordered call and each retry consumes several nodes.
    return max(100, 30 + 12 * max(0, config.max_turns_per_round) * config.num_agents
               + 14 * config.num_agents * (max(0, config.decision_retries) + 1))
