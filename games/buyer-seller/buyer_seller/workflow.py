"""Executable bargaining orchestration; export reads this same LangGraph."""

from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from a2a_engine.workflow_runtime import AgentStep, GraphProgram, GuardedAgent, require_agent_step


class GameState(TypedDict, total=False):
    round: int
    units_sold: int
    history: list[dict]
    strikes: list[dict]
    buyer_utility: float
    seller_utility: float
    last_price: float | None
    offer_action: dict
    response_action: dict
    price: float
    violation: bool
    accepted: bool


@dataclass
class GameContext:
    game: object

    def __post_init__(self):
        for role in ("seller", "buyer"):
            agent = getattr(self.game, role)
            if not isinstance(agent, GuardedAgent):
                setattr(self.game, role, GuardedAgent(agent))


async def initialize(state: GameState, runtime: Runtime[GameContext]):
    game, cfg = runtime.context.game, runtime.context.game.config
    game.events.append("game_start", data={
        "num_items": cfg.num_items, "max_rounds": cfg.max_rounds,
        "discount_factor": cfg.discount_factor,
        "enforce_monotonic_offers": cfg.enforce_monotonic_offers,
        "opening_price": game.opening_price,
    })
    return {"round": 1, "units_sold": 0, "history": [], "strikes": [],
            "buyer_utility": 0.0, "seller_utility": 0.0, "last_price": None}


def select_seller(state, context):
    return context.game.seller


def select_buyer(state, context):
    return context.game.buyer


def seller_registration(state, context):
    return registration(context, "seller")


def buyer_registration(state, context):
    return registration(context, "buyer")


def registration(context, role):
    cfg = context.game.config
    info = {"role": role, "num_items": cfg.num_items, "max_rounds": cfg.max_rounds,
            "discount_factor": cfg.discount_factor}
    if role == "seller":
        info.update(cost=cfg.seller_cost, opening=context.game.opening_price,
                    monotonic=cfg.enforce_monotonic_offers)
    else:
        info.update(value=cfg.buyer_value, patience=cfg.buyer_patience)
    return info


async def lifecycle_adapter(actor, operation, observation, state, context):
    require_agent_step()
    from .remote import RemoteBargainingAgent
    if isinstance(actor.agent, RemoteBargainingAgent):
        import asyncio
        if operation == "register":
            await asyncio.to_thread(actor.register, observation)
        else:
            await asyncio.to_thread(actor.episode_end)
    return {}


def no_observation(state, context):
    return {}


def inventory_deadline_guard(state: GameState):
    return {}


def can_continue(state: GameState, runtime: Runtime[GameContext]):
    cfg = runtime.context.game.config
    return "continue" if state["units_sold"] < cfg.num_items and state["round"] <= cfg.max_rounds else "finished"


async def start_round(state: GameState, runtime: Runtime[GameContext]):
    game = runtime.context.game
    game.events.append("round_start", data={"round": state["round"],
                       "units_remaining": game.config.num_items - state["units_sold"]})
    return {}


def seller_observation(state, context):
    return context.game._seller_observation(state["round"], state["units_sold"],
                                           state["history"], state["last_price"])


def buyer_observation(state, context):
    return context.game._buyer_observation(state["round"], state["units_sold"],
                                          state["history"], state["price"])


async def bargaining_adapter(actor, operation, observation, state, context):
    require_agent_step()
    action = await actor.act(observation, tools={})
    return {"offer_action" if actor is context.game.seller else "response_action": action}


def normalize_offer(state: GameState, runtime: Runtime[GameContext]):
    price = float(state["offer_action"]["price"])
    violation = bool(runtime.context.game.config.enforce_monotonic_offers and
                     state["last_price"] is not None and price > state["last_price"])
    if violation:
        price = state["last_price"]
    return {"price": round(price, 2), "violation": violation}


async def emit_offer(state: GameState, runtime: Runtime[GameContext]):
    runtime.context.game.events.append("offer", data={
        "round": state["round"], "speaker": "seller",
        "text": state["offer_action"].get("text") or f"I offer one unit at {state['price']:.2f}.",
        "price": state["price"], "monotonic_violation": state["violation"],
    })
    return {}


async def emit_response(state: GameState, runtime: Runtime[GameContext]):
    accepted = bool(state["response_action"]["accept"])
    runtime.context.game.events.append("response", data={
        "round": state["round"], "speaker": "buyer",
        "text": state["response_action"].get("text") or ("I accept." if accepted else "I reject."),
        "decision": "accept" if accepted else "reject", "price": state["price"],
    })
    return {"accepted": accepted}


def accepted_offer(state: GameState):
    return "accepted" if state["accepted"] else "rejected"


async def trade_one_unit(state: GameState, runtime: Runtime[GameContext]):
    cfg = runtime.context.game.config
    discount = cfg.discount_factor ** (state["round"] - 1)
    buyer_surplus = discount * (cfg.buyer_value - state["price"])
    seller_surplus = discount * (state["price"] - cfg.seller_cost)
    strike = {"unit_index": state["units_sold"] + 1, "round": state["round"],
              "price": state["price"], "discount": discount,
              "buyer_surplus": buyer_surplus, "seller_surplus": seller_surplus}
    runtime.context.game.events.append("trade", data=strike)
    return {"units_sold": state["units_sold"] + 1, "strikes": [*state["strikes"], strike],
            "buyer_utility": state["buyer_utility"] + buyer_surplus,
            "seller_utility": state["seller_utility"] + seller_surplus}


def advance_round(state: GameState):
    return {"history": [*state["history"], {"round": state["round"], "price": state["price"],
                                          "accepted": state["accepted"]}],
            "last_price": state["price"], "round": state["round"] + 1}


async def finalize(state: GameState, runtime: Runtime[GameContext]):
    from .game import DEADLINE, SOLD_OUT
    game = runtime.context.game
    reason = SOLD_OUT if state["units_sold"] >= game.config.num_items else DEADLINE
    game.events.append("game_end", data={"reason": reason, "units_sold": state["units_sold"],
                       "rounds_used": state["round"] - 1})
    return {}


def build_program():
    builder = StateGraph(GameState, context_schema=GameContext)
    for node in (initialize, inventory_deadline_guard, start_round, normalize_offer, emit_offer,
                 emit_response, trade_one_unit, advance_round, finalize):
        builder.add_node(node.__name__, node)
    builder.add_node("seller_register", AgentStep(select_seller, "register", seller_registration, lifecycle_adapter))
    builder.add_node("buyer_register", AgentStep(select_buyer, "register", buyer_registration, lifecycle_adapter))
    builder.add_node("seller_call", AgentStep(select_seller, "act", seller_observation, bargaining_adapter))
    builder.add_node("buyer_call", AgentStep(select_buyer, "act", buyer_observation, bargaining_adapter))
    builder.add_node("seller_end", AgentStep(select_seller, "episode_end", no_observation, lifecycle_adapter))
    builder.add_node("buyer_end", AgentStep(select_buyer, "episode_end", no_observation, lifecycle_adapter))
    builder.add_edge(START, "initialize")
    builder.add_edge("initialize", "seller_register")
    builder.add_edge("seller_register", "buyer_register")
    builder.add_edge("buyer_register", "inventory_deadline_guard")
    builder.add_conditional_edges("inventory_deadline_guard", can_continue,
                                 {"continue": "start_round", "finished": "finalize"})
    builder.add_edge("start_round", "seller_call")
    builder.add_edge("seller_call", "normalize_offer")
    builder.add_edge("normalize_offer", "emit_offer")
    builder.add_edge("emit_offer", "buyer_call")
    builder.add_edge("buyer_call", "emit_response")
    builder.add_conditional_edges("emit_response", accepted_offer,
                                 {"accepted": "trade_one_unit", "rejected": "advance_round"})
    builder.add_edge("trade_one_unit", "advance_round")
    builder.add_edge("advance_round", "inventory_deadline_guard")
    builder.add_edge("finalize", "seller_end")
    builder.add_edge("seller_end", "buyer_end")
    builder.add_edge("buyer_end", END)
    return GraphProgram(builder)
