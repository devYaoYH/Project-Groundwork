"""Executable semantic story: next round → private dialogue → score round."""

from dataclasses import dataclass
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from a2a_engine.workflow_runtime import (
    GraphProgram,
    AgentStep,
    GuardedAgent,
    require_agent_step,
)
from .maps import score


class GameState(TypedDict, total=False):
    round_index: int
    outcomes: list
    incoming: str
    turn: int
    route: list
    submitted: bool
    validation_feedback: str
    action: dict
    trace: object


@dataclass
class GameContext:
    game: object
    roster: object = None

    def __post_init__(self):
        for role in ("giver", "follower"):
            agent = getattr(self.game, role)
            if not isinstance(agent, GuardedAgent):
                setattr(self.game, role, GuardedAgent(agent))


def next_round(state, runtime):
    return {}


def round_destination(state, runtime):
    return (
        "play"
        if state["round_index"] < runtime.context.game.config.num_rounds
        else "finish"
    )


def begin_round(state, runtime):
    runtime.context.game.events.append(
        "round_start", data={"round": state["round_index"] + 1}
    )
    return {
        "turn": 0,
        "route": [],
        "submitted": False,
        "validation_feedback": "",
        "incoming": "New round. Establish this route using your private map.",
    }


def select_giver(state, context):
    return context.game.giver


def select_follower(state, context):
    return context.game.follower


def giver_observation(state, context):
    return context.game.observation(state, "giver")


def follower_observation(state, context):
    return context.game.observation(state, "follower")


async def invoke(actor, operation, observation, state, context):
    require_agent_step()
    action = await actor.act(observation, {})
    return {"action": action}


def publish_instruction(state, runtime):
    text = state["action"].get("text", "")
    runtime.context.game.events.append(
        "message",
        data={
            "speaker": "giver",
            "text": text,
            "round": state["round_index"] + 1,
            "parse_failed": bool(state["action"].get("parse_failed")),
        },
    )
    return {"incoming": text}


def record_drawing(state, runtime):
    game = runtime.context.game
    action = state["action"]
    text = action.get("text", "")
    game.events.append(
        "message",
        data={
            "speaker": "follower",
            "text": text,
            "round": state["round_index"] + 1,
            "parse_failed": bool(state["action"].get("parse_failed")),
        },
    )
    route, submitted, feedback = game.validate_action(action, state)
    summary = (
        f"Drawing rejected. {feedback}"
        if feedback
        else f"{'Submitted' if submitted else 'Saved'} a route with {len(route)} points."
    )
    game.events.append(
        "drawing",
        data={
            "speaker": "follower",
            "speaker_id": "follower",
            "text": summary,
            "content": [
                {"type": "text", "text": summary},
                game.drawing_image(state["round_index"], route),
            ],
            "round": state["round_index"] + 1,
            "route": route,
            "submitted": submitted,
            "validation": feedback,
            "visibility": "private",
        },
    )
    return {
        "route": route,
        "submitted": submitted,
        "turn": state["turn"] + 1,
        "incoming": text,
        "validation_feedback": feedback,
    }


def exchange_destination(state, runtime):
    return (
        "done"
        if state["submitted"] or state["turn"] >= runtime.context.game.config.max_turns
        else "continue"
    )


def close_round(state, runtime):
    game = runtime.context.game
    outcome = score(
        state["route"], game.tasks[state["round_index"]], submitted=state["submitted"]
    )
    outcome.update(
        round=state["round_index"] + 1,
        item_id=game.tasks[state["round_index"]]["item_id"],
        route=state["route"],
        turns_used=state["turn"],
    )
    summary = (
        f"Final follower route: {'submitted' if state['submitted'] else 'unsubmitted'}, "
        f"{len(state['route'])} points. "
        f"{'Success' if outcome['success'] else 'Unsuccessful'}."
    )
    outcome.update(
        speaker="follower",
        visibility="private",
        text=summary,
        content=[
            {"type": "text", "text": summary},
            game.drawing_image(state["round_index"], state["route"]),
        ],
    )
    game.events.append("round_end", data=outcome)
    return {
        "outcomes": [*state["outcomes"], outcome],
        "round_index": state["round_index"] + 1,
    }


def finalize(state, runtime):
    return {"trace": runtime.context.game.trace(state)}


def round_program():
    builder = StateGraph(GameState, context_schema=GameContext)
    builder.add_node("begin_round", begin_round)
    builder.add_node("giver", AgentStep(select_giver, "act", giver_observation, invoke))
    builder.add_node("publish_instruction", publish_instruction)
    builder.add_node(
        "follower", AgentStep(select_follower, "act", follower_observation, invoke)
    )
    builder.add_node("record_drawing", record_drawing)
    builder.add_edge(START, "begin_round")
    builder.add_edge("begin_round", "giver")
    builder.add_edge("giver", "publish_instruction")
    builder.add_edge("publish_instruction", "follower")
    builder.add_edge("follower", "record_drawing")
    builder.add_conditional_edges(
        "record_drawing", exchange_destination, {"done": END, "continue": "giver"}
    )
    return GraphProgram(builder)


def build_program():
    builder = StateGraph(GameState, context_schema=GameContext)
    builder.add_node("next_round", next_round)
    builder.add_node("round", round_program().compiled)
    builder.add_node("score_round", close_round)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "next_round")
    builder.add_conditional_edges(
        "next_round", round_destination, {"play": "round", "finish": "finalize"}
    )
    builder.add_edge("round", "score_round")
    builder.add_edge("score_round", "next_round")
    builder.add_edge("finalize", END)
    return GraphProgram(builder)
