"""Executable story: repeat task rounds; report, discuss, report, commit, score."""

from dataclasses import dataclass
import json
import math
from typing import TypedDict
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from a2a_engine.workflow_runtime import (
    AgentStep,
    GraphProgram,
    GuardedAgent,
    require_agent_step,
)


class State(TypedDict, total=False):
    round_index: int
    results: list
    phase: str
    seat: int
    phase_turn: int
    phase_limit: int
    discussion: list
    commitment_messages: list
    proposal: dict | None
    endorsements: list
    pre_reports: dict
    post_reports: dict
    action: dict
    choice: str | None


@dataclass
class GameContext:
    game: object
    roster: object = None

    def __post_init__(self):
        self.game.agents = [
            a if isinstance(a, GuardedAgent) else GuardedAgent(a)
            for a in self.game.agents
        ]


def next_round(state):
    return {}


def round_route(state, runtime: Runtime[GameContext]):
    return "next" if state["round_index"] < len(runtime.context.game.rows) else "done"


def begin_round(state, runtime: Runtime[GameContext]):
    game = runtime.context.game
    game.events.append(
        "round_start",
        data={
            "round": state["round_index"] + 1,
            "item_id": game.rows[state["round_index"]]["item_id"],
        },
    )
    return {
        "discussion": [],
        "commitment_messages": [],
        "proposal": None,
        "endorsements": [],
        "pre_reports": {},
        "post_reports": {},
        "choice": None,
    }


def phase_start(phase):
    def begin(state, runtime: Runtime[GameContext]):
        cfg = runtime.context.game.config
        cycles = (
            cfg.discussion_cycles
            if phase == "discussion"
            else cfg.commitment_cycles
            if phase == "commitment"
            else 1
        )
        return {"phase": phase, "seat": 0, "phase_turn": 0, "phase_limit": 4 * cycles}

    return begin


def participant(state, context):
    return context.game.agents[state["seat"]]


def observation(state, context):
    obs = context.game.observation(state)
    context.game.events.append(
        "observation",
        data={
            "speaker": participant(state, context).name,
            "round": obs["round"],
            "phase": obs["phase"],
            "content": json.dumps(obs, ensure_ascii=False),
            "visibility": "private",
        },
    )
    return obs


async def act(actor, operation, obs, state, context):
    require_agent_step()
    return {"action": await actor.act(obs, {})}


def valid_report(action):
    scores = action.get("scores")
    return (
        (
            action.get("choice") in "ABC"
            if isinstance(action.get("choice"), str) and len(action["choice"]) == 1
            else False
        )
        and isinstance(scores, dict)
        and set(scores) == set("ABC")
        and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            for v in scores.values()
        )
    )


def record_turn(state, runtime: Runtime[GameContext]):
    game, action, phase, seat = (
        runtime.context.game,
        state["action"],
        state["phase"],
        state["seat"],
    )
    updates = {"phase_turn": state["phase_turn"] + 1, "seat": (seat + 1) % 4}
    valid = isinstance(action, dict) and not action.get("parse_failed")
    if phase in ("pre_report", "post_report"):
        valid = valid and valid_report(action)
        if valid:
            key = "pre_reports" if phase == "pre_report" else "post_reports"
            report = {"choice": action["choice"], "scores": action["scores"]}
            updates[key] = {**state[key], seat: report}
            game.events.append(
                "sealed_report",
                data={
                    "round": state["round_index"] + 1,
                    "phase": phase,
                    "participant": seat + 1,
                    "report": report,
                    "speaker": game.agents[seat].name,
                    "text": f"Sealed {phase}: choice {report['choice']}; scores {', '.join(f'{c}={report['scores'][c]:g}' for c in 'ABC')}",
                    "visibility": "private",
                },
            )
    else:
        kind = action.get("kind")
        text = action.get("text", "")
        valid = (
            valid
            and isinstance(text, str)
            and len(text) <= game.config.max_message_chars
        )
        if phase == "discussion":
            valid = valid and kind in ("say", "pass")
            if valid and kind == "say":
                message = {
                    "speaker": game.agents[seat].name,
                    "text": text,
                    "round": state["round_index"] + 1,
                    "phase": phase,
                }
                updates["discussion"] = [*state["discussion"], message]
                game.events.append("message", data=message)
        else:
            candidate = action.get("candidate")
            valid = valid and kind in ("propose", "endorse", "pass")
            if kind in ("propose", "endorse"):
                valid = valid and candidate in ("A", "B", "C")
            if valid and kind == "endorse":
                valid = (
                    state["proposal"] is not None
                    and candidate == state["proposal"]["candidate"]
                )
            if valid and kind == "propose":
                updates.update(
                    proposal={"candidate": candidate, "proposer": seat + 1},
                    endorsements=[],
                )
            elif valid and kind == "endorse":
                endorsements = sorted(set([*state["endorsements"], seat + 1]))
                updates["endorsements"] = endorsements
                if len(endorsements) == 4:
                    updates["choice"] = candidate
            if valid:
                message = {
                    "speaker": game.agents[seat].name,
                    "text": f"{kind.upper()} {candidate or ''}. {text}".strip(),
                    "round": state["round_index"] + 1,
                    "phase": phase,
                }
                updates["commitment_messages"] = [
                    *state["commitment_messages"],
                    message,
                ]
                game.events.append("message", data=message)
    if not valid:
        # No oracle/default replacement; invalid turns consume the configured phase budget.
        game.events.append(
            "invalid_action",
            data={
                "round": state["round_index"] + 1,
                "phase": phase,
                "participant": seat + 1,
            },
        )
    return updates


def phase_route(state):
    return (
        "done"
        if state["phase_turn"] >= state["phase_limit"]
        or (state["phase"] == "commitment" and state["choice"] is not None)
        else "next"
    )


def phase_program(phase):
    builder = StateGraph(State, context_schema=GameContext)
    builder.add_node("begin", phase_start(phase))
    builder.add_node("participant", AgentStep(participant, "act", observation, act))
    builder.add_node("record_turn", record_turn)
    builder.add_edge(START, "begin")
    builder.add_edge("begin", "participant")
    builder.add_edge("participant", "record_turn")
    builder.add_conditional_edges(
        "record_turn", phase_route, {"next": "participant", "done": END}
    )
    return GraphProgram(builder)


def score_round(state, runtime: Runtime[GameContext]):
    game = runtime.context.game
    oracle = game.rows[state["round_index"]]["oracle_result"]
    scores, choice = oracle["pooled_scores"], state["choice"]
    pre, post = state["pre_reports"], state["post_reports"]
    result = {
        "round": state["round_index"] + 1,
        "item_id": game.rows[state["round_index"]]["item_id"],
        "choice": choice,
        "oracle_choice": oracle["pooled_ranking"][0],
        "pooled_scores": scores,
        "optimal_choice": choice == oracle["pooled_ranking"][0],
        "regret": max(scores.values()) - scores[choice] if choice else None,
        "pre_reports": pre,
        "post_reports": post,
        "preference_revisions": sum(
            pre[i]["choice"] != post[i]["choice"] for i in pre.keys() & post.keys()
        ),
        "post_score_absolute_error": sum(
            abs(r["scores"][c] - scores[c]) for r in post.values() for c in "ABC"
        ),
        "valid_post_reports": len(post),
        "termination": "unanimous_commitment" if choice else "no_consensus",
    }
    game.events.append("round_end", data=result)
    return {
        "results": [*state["results"], result],
        "round_index": state["round_index"] + 1,
    }


def round_program():
    builder = StateGraph(State, context_schema=GameContext)
    builder.add_node("receive_packets", begin_round)
    for phase in ("pre_report", "discussion", "post_report", "commitment"):
        builder.add_node(phase, phase_program(phase).compiled)
    builder.add_node("score_round", score_round)
    chain = [
        START,
        "receive_packets",
        "pre_report",
        "discussion",
        "post_report",
        "commitment",
        "score_round",
        END,
    ]
    for left, right in zip(chain, chain[1:]):
        builder.add_edge(left, right)
    return GraphProgram(builder)


def finalize(state, runtime: Runtime[GameContext]):
    runtime.context.game.events.append(
        "game_end", data={"rounds_completed": len(state["results"])}
    )
    return {}


def build_program():
    builder = StateGraph(State, context_schema=GameContext)
    builder.add_node("next_round", next_round)
    builder.add_node("round", round_program().compiled)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "next_round")
    builder.add_conditional_edges(
        "next_round", round_route, {"next": "round", "done": "finalize"}
    )
    builder.add_edge("round", "next_round")
    builder.add_edge("finalize", END)
    return GraphProgram(builder)
