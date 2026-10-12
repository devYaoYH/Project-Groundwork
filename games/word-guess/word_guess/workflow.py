"""Word Guess's actual control flow, shared by execution and structural export."""

from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from a2a_engine.workflow_runtime import AgentStep, GraphProgram, GuardedAgent, require_agent_step


class GameState(TypedDict, total=False):
    history: list[dict[str, str]]
    turns_used: int
    guessed_word: str | None
    won: bool
    guess_action: dict
    host_action: dict


@dataclass
class GameContext:
    game: object
    roster: object = None

    def __post_init__(self):
        for name in ("guesser", "host"):
            agent = getattr(self.game, name)
            if not isinstance(agent, GuardedAgent):
                setattr(self.game, name, GuardedAgent(agent))


def check_turn(state: GameState):
    return {}


def can_continue(state: GameState, runtime: Runtime[GameContext]):
    return "continue" if not state["won"] and state["turns_used"] < runtime.context.game.config.max_turns else "finished"


def select_guesser(state, context):
    return context.game.guesser


def select_host(state, context):
    return context.game.host


def guesser_observation(state, context):
    return {"history": list(state["history"]), "turns_remaining": context.game.config.max_turns - state["turns_used"]}


def host_observation(state, context):
    return {"history": list(state["history"][:-1]), "last_guesser": state["guess_action"]["text"]}


async def word_guess_adapter(actor, operation, observation, state, context):
    require_agent_step()
    action = await actor.act(observation, tools={})
    return {"guess_action" if actor is context.game.guesser else "host_action": action}


def record_guess(state: GameState, runtime: Runtime[GameContext]):
    message = {"speaker": "guesser", "text": state["guess_action"]["text"]}
    runtime.context.game.events.append("message", data=message)
    return {"history": [*state["history"], message], "turns_used": state["turns_used"] + 1}


def record_reply(state: GameState, runtime: Runtime[GameContext]):
    message = {"speaker": "host", "text": state["host_action"]["text"]}
    runtime.context.game.events.append("message", data=message)
    return {"history": [*state["history"], message]}


def evaluate_exchange(state: GameState):
    from .game import _extract_guess, _is_correct
    guess = _extract_guess(state["guess_action"]["text"])
    return {"guessed_word": guess or state["guessed_word"],
            "won": bool(guess and _is_correct(state["host_action"]["text"]))}


def finalize(state: GameState, runtime: Runtime[GameContext]):
    runtime.context.game.events.append("game_end", data={"won": state["won"], "turns_used": state["turns_used"]})
    return {}


def build_program():
    builder = StateGraph(GameState, context_schema=GameContext)
    builder.add_node("check_turn", check_turn)
    builder.add_node("guesser_call", AgentStep(select_guesser, "act", guesser_observation, word_guess_adapter))
    builder.add_node("record_guess", record_guess)
    builder.add_node("host_call", AgentStep(select_host, "act", host_observation, word_guess_adapter))
    builder.add_node("record_reply", record_reply)
    builder.add_node("evaluate_exchange", evaluate_exchange)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "check_turn")
    builder.add_conditional_edges("check_turn", can_continue, {"continue": "guesser_call", "finished": "finalize"})
    builder.add_edge("guesser_call", "record_guess")
    builder.add_edge("record_guess", "host_call")
    builder.add_edge("host_call", "record_reply")
    builder.add_edge("record_reply", "evaluate_exchange")
    builder.add_edge("evaluate_exchange", "check_turn")
    builder.add_edge("finalize", END)
    return GraphProgram(builder)
