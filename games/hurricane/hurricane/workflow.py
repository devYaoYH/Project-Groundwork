"""Executable story: each round runs experiment, discussion and decision phases; then landfall."""

from dataclasses import dataclass
import json
from typing import TypedDict
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from a2a_engine.workflow_runtime import (
    AgentStep,
    GraphProgram,
    GuardedAgent,
    require_agent_step,
)

PHASES = ("experiment", "discussion", "decision")


class State(TypedDict, total=False):
    round_index: int
    phase: str
    seat: int
    phase_turn: int
    phase_limit: int
    action: dict
    readings: dict
    probe_log: list
    shared_data: list
    estimates: dict
    recommendations: list
    discussion: list
    orders: list
    evacuated: dict
    recon_remaining: int
    invalid_actions: int
    result: dict


@dataclass
class GameContext:
    game: object
    roster: object = None

    def __post_init__(self):
        self.game.agents = [
            a if isinstance(a, GuardedAgent) else GuardedAgent(a) for a in self.game.agents
        ]


def recursion_limit(config):
    turns = config.num_agents * (2 + config.discussion_cycles)
    return 20 + config.num_rounds * (2 * turns + 12)


def next_round(state):
    return {}


def round_route(state, runtime: Runtime[GameContext]):
    return "next" if state["round_index"] < runtime.context.game.config.num_rounds else "done"


def begin_round(state, runtime: Runtime[GameContext]):
    game = runtime.context.game
    round_number = state["round_index"] + 1
    game.events.append(
        "round_start",
        data={
            "round": round_number,
            "rounds_after_this": game.config.num_rounds - round_number,
            "order_effective": game.config.order_effective(round_number),
            "recon_remaining": state["recon_remaining"],
        },
    )
    return {}


def phase_start(phase):
    def begin(state, runtime: Runtime[GameContext]):
        cfg = runtime.context.game.config
        cycles = cfg.discussion_cycles if phase == "discussion" else 1
        return {
            "phase": phase,
            "seat": 0,
            "phase_turn": 0,
            "phase_limit": cfg.num_agents * cycles,
        }

    return begin


def manager(state, context):
    return context.game.agents[state["seat"]]


def observation(state, context):
    obs = context.game.observation(state)
    context.game.events.append(
        "observation",
        data={
            "speaker": manager(state, context).name,
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


def zone_list(value, num_zones):
    if not isinstance(value, list) or not value:
        return None
    zones = []
    for zone in value:
        if isinstance(zone, bool) or not isinstance(zone, int) or not 0 <= zone < num_zones:
            return None
        if zone not in zones:
            zones.append(zone)
    return zones


def record_experiment(state, game, seat, action, updates):
    from .environment import reading

    kind, round_number = action.get("kind"), state["round_index"] + 1
    name = game.agents[seat].name
    if kind == "pass":
        game.events.append(
            "no_probe",
            data={"round": round_number, "participant": seat + 1, "speaker": name,
                  "text": "Ran no instrument this round."},
        )
        return True
    instrument = action.get("instrument")
    if kind != "probe" or instrument not in ("recon", "model"):
        return False
    if instrument == "recon":
        if state["recon_remaining"] < 1:
            return False
        updates["recon_remaining"] = state["recon_remaining"] - 1
    ensemble = reading(game.config, round_number, seat, instrument)
    row = {"round": round_number, "instrument": instrument, "ensemble": ensemble, "shared": False}
    updates["readings"] = {**state["readings"], seat: [*state["readings"].get(seat, []), row]}
    updates["probe_log"] = [
        *state["probe_log"],
        {"round": round_number, "participant": seat + 1, "instrument": instrument},
    ]
    game.events.append(
        "probe",
        data={
            "round": round_number,
            "participant": seat + 1,
            "instrument": instrument,
            "ensemble": ensemble,
            "speaker": name,
            "text": f"{instrument} ensemble of landfall zones: {ensemble}",
            "visibility": "private",
        },
    )
    return True


def record_discussion(state, game, seat, action, updates):
    cfg, kind, round_number = game.config, action.get("kind"), state["round_index"] + 1
    name = game.agents[seat].name
    if kind == "pass":
        return True
    text, share = action.get("text", ""), action.get("share", False)
    estimate, recommend = action.get("estimate"), action.get("recommend")
    if kind != "say" or not isinstance(text, str) or len(text) > cfg.max_message_chars:
        return False
    if not isinstance(share, bool):
        return False
    if estimate is not None and (
        isinstance(estimate, bool) or not isinstance(estimate, int) or not 0 <= estimate < cfg.num_zones
    ):
        return False
    if recommend is not None:
        recommend = zone_list(recommend, cfg.num_zones)
        if recommend is None:
            return False
    message = {
        "speaker": name,
        "text": text,
        "round": round_number,
        "phase": "discussion",
        "participant": seat + 1,
        "shared_data": share,
        "estimate": estimate,
        "recommend": recommend,
    }
    updates["discussion"] = [*state["discussion"], message]
    game.events.append("message", data=message)
    if share:
        mine = state["readings"].get(seat, [])
        disclosed = [r for r in mine if not r["shared"]]
        if disclosed:
            updates["readings"] = {
                **state["readings"],
                seat: [{**r, "shared": True} for r in mine],
            }
            updates["shared_data"] = [
                *state["shared_data"],
                *(
                    {"round": r["round"], "participant": seat + 1,
                     "instrument": r["instrument"], "ensemble": r["ensemble"]}
                    for r in disclosed
                ),
            ]
            game.events.append(
                "disclosure",
                data={
                    "round": round_number,
                    "participant": seat + 1,
                    "speaker": name,
                    "readings": disclosed,
                    "text": "Shared data: " + "; ".join(
                        f"round {r['round']} {r['instrument']} {r['ensemble']}" for r in disclosed
                    ),
                },
            )
    if estimate is not None:
        updates["estimates"] = {**state["estimates"], seat: estimate}
    if recommend:
        updates["recommendations"] = [
            *state["recommendations"],
            {"round": round_number, "participant": seat + 1, "zones": recommend},
        ]
    return True


def record_decision(state, game, seat, action, updates):
    cfg, kind, round_number = game.config, action.get("kind"), state["round_index"] + 1
    name = game.agents[seat].name
    if kind == "hold":
        game.events.append(
            "hold",
            data={"round": round_number, "participant": seat + 1, "speaker": name,
                  "text": "Holding: no new evacuation orders."},
        )
        return True
    if kind != "evacuate":
        return False
    zones = zone_list(action.get("zones"), cfg.num_zones)
    if zones is None or any(cfg.sector_of(z) != seat or z in state["evacuated"] for z in zones):
        return False
    effective = cfg.order_effective(round_number)
    orders = [
        {"round": round_number, "participant": seat + 1, "zone": z, "effective": effective}
        for z in zones
    ]
    updates["orders"] = [*state["orders"], *orders]
    updates["evacuated"] = {**state["evacuated"], **{o["zone"]: o for o in orders}}
    game.events.append(
        "evacuation_order",
        data={
            "round": round_number,
            "participant": seat + 1,
            "zones": zones,
            "effective": effective,
            "population": sum(cfg.populations[z] for z in zones),
            "speaker": name,
            "text": f"Ordered evacuation of zones {zones}"
            + ("." if effective else " (too late to protect anyone)."),
        },
    )
    return True


RECORDERS = {
    "experiment": record_experiment,
    "discussion": record_discussion,
    "decision": record_decision,
}


def record_turn(state, runtime: Runtime[GameContext]):
    game, action, phase, seat = runtime.context.game, state["action"], state["phase"], state["seat"]
    updates = {"phase_turn": state["phase_turn"] + 1, "seat": (seat + 1) % game.config.num_agents}
    valid = isinstance(action, dict) and not action.get("parse_failed")
    if valid:
        valid = RECORDERS[phase](state, game, seat, action, updates)
    if not valid:
        # No oracle/default replacement; an invalid turn consumes the configured phase budget.
        updates["invalid_actions"] = state["invalid_actions"] + 1
        game.events.append(
            "invalid_action",
            data={"round": state["round_index"] + 1, "phase": phase, "participant": seat + 1},
        )
    return updates


def phase_route(state):
    return "done" if state["phase_turn"] >= state["phase_limit"] else "next"


def phase_program(phase):
    builder = StateGraph(State, context_schema=GameContext)
    builder.add_node("begin", phase_start(phase))
    builder.add_node("manager", AgentStep(manager, "act", observation, act))
    builder.add_node("record_turn", record_turn)
    builder.add_edge(START, "begin")
    builder.add_edge("begin", "manager")
    builder.add_edge("manager", "record_turn")
    builder.add_conditional_edges("record_turn", phase_route, {"next": "manager", "done": END})
    return GraphProgram(builder)


def close_round(state, runtime: Runtime[GameContext]):
    game = runtime.context.game
    game.events.append(
        "round_end",
        data={
            "round": state["round_index"] + 1,
            "recon_remaining": state["recon_remaining"],
            "orders": len(state["orders"]),
            "readings_shared": len(state["shared_data"]),
        },
    )
    return {"round_index": state["round_index"] + 1}


def round_program():
    builder = StateGraph(State, context_schema=GameContext)
    builder.add_node("begin_round", begin_round)
    for phase in PHASES:
        builder.add_node(phase, phase_program(phase).compiled)
    builder.add_node("close_round", close_round)
    chain = [START, "begin_round", *PHASES, "close_round", END]
    for left, right in zip(chain, chain[1:]):
        builder.add_edge(left, right)
    return GraphProgram(builder)


def landfall(state, runtime: Runtime[GameContext]):
    game = runtime.context.game
    result = game.landfall(state)
    game.events.append(
        "landfall",
        data={
            **result,
            "text": (
                f"Landfall at zone {result['landfall_zone']}; zones {result['hit_zones']} hit. "
                f"Protected {result['population_protected']}, exposed {result['population_exposed']}, "
                f"needlessly evacuated {result['population_needlessly_evacuated']}. "
                f"Total cost {result['total_cost']:g} (oracle {result['oracle_cost']:g})."
            ),
        },
    )
    return {"result": result}


def finalize(state, runtime: Runtime[GameContext]):
    runtime.context.game.events.append(
        "game_end",
        data={"rounds_completed": state["round_index"], "total_cost": state["result"]["total_cost"]},
    )
    return {}


def build_program():
    builder = StateGraph(State, context_schema=GameContext)
    builder.add_node("next_round", next_round)
    builder.add_node("round", round_program().compiled)
    builder.add_node("landfall", landfall)
    builder.add_node("finalize", finalize)
    builder.add_edge(START, "next_round")
    builder.add_conditional_edges("next_round", round_route, {"next": "round", "done": "landfall"})
    builder.add_edge("round", "next_round")
    builder.add_edge("landfall", "finalize")
    builder.add_edge("finalize", END)
    return GraphProgram(builder)
