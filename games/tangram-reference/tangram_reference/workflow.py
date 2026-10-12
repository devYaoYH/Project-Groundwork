"""Executable play story: repeat a bounded reference trial with the same dyad."""

from dataclasses import dataclass
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.runtime import Runtime
from a2a_engine.workflow_runtime import (
    GraphProgram,
    AgentStep,
    GuardedAgent,
    require_agent_step,
)


class GameState(TypedDict, total=False):
    round: int
    exchange: int
    history: list[dict]
    outcomes: list[dict]
    board: list
    submitted: bool
    validation_error: str | None
    director_action: dict
    matcher_action: dict


@dataclass
class GameContext:
    game: object
    roster: object = None

    def __post_init__(self):
        for r in ("director", "matcher"):
            if not isinstance(getattr(self.game, r), GuardedAgent):
                setattr(self.game, r, GuardedAgent(getattr(self.game, r)))


def next_round(state):
    return {}


def round_route(state, runtime: Runtime[GameContext]):
    return (
        "trial" if state["round"] <= runtime.context.game.config.num_rounds else "done"
    )


def start_trial(state, runtime: Runtime[GameContext]):
    runtime.context.game.events.append("round_start", data={"round": state["round"]})
    return {
        "exchange": 1,
        "board": [None] * 4,
        "submitted": False,
        "validation_error": None,
    }


def select_director(state, context):
    return context.game.director


def select_matcher(state, context):
    return context.game.matcher


def director_observation(state, context):
    return context.game.observation(state, "director")


def matcher_observation(state, context):
    return context.game.observation(state, "matcher")


async def invoke(actor, operation, observation, state, context):
    require_agent_step()
    context.game.events.append(
        "observation",
        data={
            "speaker": actor.name,
            "round": state["round"],
            "exchange": state["exchange"],
            "visibility": "private",
            "content": observation["content"],
        },
    )
    action = await actor.act(observation, {})
    return {f"{actor.name}_action": action}


def publish_director(state, runtime: Runtime[GameContext]):
    action = state["director_action"]
    message = {"speaker": "director", "text": str(action.get("text", ""))}
    runtime.context.game.events.append(
        "message",
        data={
            **message,
            "round": state["round"],
            "parse_failed": bool(action.get("parse_failed")),
        },
    )
    return {"history": [*state["history"], message]}


def record_matcher(state, runtime: Runtime[GameContext]):
    game = runtime.context.game
    action = state["matcher_action"]
    board = list(state["board"])
    error = None
    handles = game.rounds[state["round"] - 1]["handles"]
    proposed = action.get("arrangement")
    if proposed is not None:
        if (
            not isinstance(proposed, list)
            or len(proposed) != 4
            or set(str(x) for x in proposed) != set(handles)
        ):
            error = "arrangement must contain each current handle exactly once"
            game.events.append(
                "invalid_action",
                data={"round": state["round"], "seat": "matcher", "reason": error},
            )
        else:
            board = list(proposed)
    submit = (
        action.get("submit") is True
        and error is None
        and all(x is not None for x in board)
    )
    if action.get("submit") is True and not submit and error is None:
        error = "fill all four slots before submitting"
    message = {"speaker": "matcher", "text": str(action.get("text", ""))}
    game.events.append(
        "message",
        data={
            **message,
            "round": state["round"],
            "parse_failed": bool(action.get("parse_failed")),
        },
    )
    game.events.append(
        "placement",
        data={
            "round": state["round"],
            "visibility": "private",
            "seat": "matcher",
            "arrangement": board,
            "submitted": submit,
        },
    )
    return {
        "history": [*state["history"], message],
        "board": board,
        "submitted": submit,
        "validation_error": error,
        "exchange": state["exchange"] + 1,
    }


def trial_route(state, runtime: Runtime[GameContext]):
    return (
        "score"
        if state["submitted"]
        or state["exchange"] > runtime.context.game.config.max_exchanges
        else "clarify"
    )


def score_trial(state, runtime: Runtime[GameContext]):
    game = runtime.context.game
    row = game.rounds[state["round"] - 1]
    mapping = dict(zip(row["handles"], row["gallery"], strict=True))
    chosen = [mapping.get(h) for h in state["board"]]
    correct = [a == b for a, b in zip(chosen, row["target"], strict=True)]
    outcome = {
        "round": state["round"],
        "accuracy": sum(correct) / 4,
        "success": all(correct),
        "submitted": state["submitted"],
        "reason": "submitted" if state["submitted"] else "deadline",
        "exchanges": state["exchange"] - 1,
        "board": list(state["board"]),
        "chosen": chosen,
        "target": row["target"],
        "mismatched_slots": [i + 1 for i, c in enumerate(correct) if not c],
    }
    game.events.append("round_end", data=outcome)
    feedback = {
        "speaker": "environment",
        "text": f"Round {state['round']} ended: {sum(correct)}/4 positions correct; mismatched slots {outcome['mismatched_slots']}.",
    }
    game.events.append("feedback", data={**feedback, "round": state["round"]})
    return {
        "outcomes": [*state["outcomes"], outcome],
        "history": [*state["history"], feedback],
        "round": state["round"] + 1,
    }


def trial_program():
    b = StateGraph(GameState, context_schema=GameContext)
    b.add_node("start_trial", start_trial)
    b.add_node(
        "director_describes",
        AgentStep(select_director, "act", director_observation, invoke),
    )
    b.add_node("publish_description", publish_director)
    b.add_node(
        "matcher_arranges",
        AgentStep(select_matcher, "act", matcher_observation, invoke),
    )
    b.add_node("record_arrangement", record_matcher)
    b.add_node("score_trial", score_trial)
    b.add_edge(START, "start_trial")
    b.add_edge("start_trial", "director_describes")
    b.add_edge("director_describes", "publish_description")
    b.add_edge("publish_description", "matcher_arranges")
    b.add_edge("matcher_arranges", "record_arrangement")
    b.add_conditional_edges(
        "record_arrangement",
        trial_route,
        {"score": "score_trial", "clarify": "director_describes"},
    )
    b.add_edge("score_trial", END)
    return GraphProgram(b)


def build_program():
    b = StateGraph(GameState, context_schema=GameContext)
    b.add_node("next_round", next_round)
    b.add_node("reference_trial", trial_program().compiled)
    b.add_edge(START, "next_round")
    b.add_conditional_edges(
        "next_round", round_route, {"trial": "reference_trial", "done": END}
    )
    b.add_edge("reference_trial", "next_round")
    return GraphProgram(b)
