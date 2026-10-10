"""BuyerSellerGame — sequential multi-item bargaining under asymmetric information.

Implements `SPEC.md`: seller-led alternating offers over ``k`` identical items,
with a per-round discount factor ``delta`` that makes delay costly for both
sides, and a hard deadline ``T``.

Two spec details are worth stating because they are choices, not restatements:

**A round is one offer/response exchange, and ``t`` advances after every round,
accepts included.** The spec's diagram loops back to "S offers again" after a
sale without saying whether the clock moves. Advancing on accepts is what makes
the payoff formula ``delta**(t-1)`` well defined per unit — otherwise multiple
units could settle at the same ``t`` and the discount would stop discriminating
between fast and slow agreement. It also caps the environment at ``T`` exchanges total,
which is what the deadline is for.

**Monotonicity violations are clamped and recorded, not rejected.** With
``enforce_monotonic_offers``, §5's optional ``p_t <= p_{t-1}`` rule, an LLM
seller that raises its price gets the offer clamped to the previous price and a
``monotonic_violation`` flag on the event. Erroring out instead would discard an
otherwise complete trajectory over a formatting-level mistake, and the flag
keeps the violation visible to analysis.

Information asymmetry is enforced structurally: the observation dicts are built
by :meth:`BuyerSellerGame._seller_observation` and ``_buyer_observation``, and
neither ever contains the other side's private value. An agent cannot see what
it was not handed.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from typing import Any

from pydantic import Field, model_validator

from a2a_engine import EventLog, EpisodeConfigBase, EpisodeTrace, register_environment
from a2a_engine._context import current_conversation_id
from a2a_engine.llm.factory import make_llm_client
from a2a_engine.tracing_otel import get_tracer

from buyer_seller.agents import BuyerAgent, SellerAgent
from buyer_seller.declaration import DECLARATION

# Terminal reasons recorded in final_state.
SOLD_OUT = "inventory_exhausted"
DEADLINE = "deadline_reached"


def undiscounted_total_surplus(
    seller_cost: float, buyer_value: float, num_items: int
) -> float:
    """Informational total surplus before the configured discount is applied.

    This is not the configured-game oracle optimum. The item-bank oracle is the
    discounted first-best for the complete frozen item tuple, including
    ``discount_factor``.
    """

    return max(0.0, buyer_value - seller_cost) * num_items


def best_joint_utility(
    seller_cost: float, buyer_value: float, num_items: int, discount_factor: float
) -> float:
    """First-best under the row's discount and the release-fixed horizon."""

    gains = max(0.0, buyer_value - seller_cost)
    return sum(discount_factor ** index * gains for index in range(num_items))


class BuyerSellerConfig(EpisodeConfigBase):
    """Config for the buyer-seller bargaining environment.

    ``seller_cost`` and ``buyer_value`` are the private values c and v. They are
    recorded in the trace — the record is not an observation — but never reach
    the opposing agent.
    """

    environment_id: str = "buyer_seller"
    num_agents: int = 2

    num_items: int = Field(default=3, ge=1, description="k: identical units for sale")
    seller_cost: float = Field(default=10.0, ge=0, description="c: seller reservation cost per unit")
    buyer_value: float = Field(default=30.0, ge=0, description="v: buyer max valuation per unit")
    max_rounds: int = Field(default=10, ge=1, description="T: deadline in rounds")
    discount_factor: float = Field(default=0.9, gt=0, le=1.0, description="delta")

    enforce_monotonic_offers: bool = False
    #: Seller's first ask. Defaults to a markup over cost, since the seller does
    #: not know v and cannot anchor on it.
    opening_price: float | None = None
    #: Scripted-agent knob: the buyer holds out until price <= buyer_value * this.
    buyer_patience: float = Field(default=0.7, gt=0, le=1.0)

    @model_validator(mode="after")
    def _check_surplus(self) -> "BuyerSellerConfig":
        if self.buyer_value < self.seller_cost:
            # Legal and interesting — it is the no-gains-from-trade control
            # condition — but silently producing all-zero metrics confuses
            # analysis, so make the intent explicit in the trace.
            self.extra.setdefault("no_gains_from_trade", True)
        return self


# ---------------------------------------------------------------------------
# Scripted agents: used by --dry-run and --smoke-test, no LLM or API key.
# ---------------------------------------------------------------------------


def _scripted_span(name: str):
    span_cm = get_tracer().start_as_current_span(f"invoke_agent {name}")
    return span_cm


class _ScriptedSeller:
    """Concedes linearly from the opening ask toward cost as the deadline nears.

    Deterministic given the config, so a smoke test produces the same trajectory
    every time and a diff in the trace means a real behavior change.
    """

    name = "seller"

    def __init__(self, cost: float, opening: float, max_rounds: int) -> None:
        self.cost = cost
        self.opening = opening
        self.max_rounds = max_rounds

    async def act(self, observation, tools):
        with _scripted_span(self.name) as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", self.name)
            conv_id = current_conversation_id.get()
            if conv_id:
                span.set_attribute("gen_ai.conversation.id", conv_id)
            t = observation["round"]
            # Linear concession: full markup at t=1, down to cost at the deadline.
            progress = (t - 1) / max(1, self.max_rounds)
            price = self.cost + (self.opening - self.cost) * (1 - progress)
            price = round(max(price, self.cost), 2)
            return {"price": price, "text": f"I offer one unit at {price:.2f}."}


class _ScriptedBuyer:
    """Holds out for a target price, then concedes as the deadline approaches.

    Never accepts above ``v`` — that would be a negative-surplus trade, which no
    rational buyer makes and which would make the efficiency metric meaningless.
    """

    name = "buyer"

    def __init__(self, value: float, patience: float, max_rounds: int) -> None:
        self.value = value
        self.target = value * patience
        self.max_rounds = max_rounds

    async def act(self, observation, tools):
        with _scripted_span(self.name) as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", self.name)
            conv_id = current_conversation_id.get()
            if conv_id:
                span.set_attribute("gen_ai.conversation.id", conv_id)
            price = observation["current_offer"]
            t = observation["round"]
            if price > self.value:
                return {"accept": False, "text": f"{price:.2f} is above my value; I reject."}
            # Endgame: a positive-surplus trade beats the zero payoff of running
            # out of rounds, so drop the target in the last two rounds.
            endgame = t >= self.max_rounds - 1
            accept = price <= self.target or endgame
            text = (f"I accept at {price:.2f}." if accept
                    else f"{price:.2f} is still too high; I reject.")
            return {"accept": accept, "text": text}


# ---------------------------------------------------------------------------
# Environment.
# ---------------------------------------------------------------------------


class BuyerSellerGame:
    """Seller-led alternating-offer bargaining over k identical units."""

    def __init__(self, config: dict | BuyerSellerConfig, dry_run: bool = False, *, runtime_manager=None, runtime_context=None) -> None:
        self.config = (
            config if isinstance(config, BuyerSellerConfig) else BuyerSellerConfig(**config)
        )
        self.dry_run = dry_run
        self.rng = random.Random(self.config.seed)
        self.events = EventLog.from_config(self.config)
        self.opening_price = (
            self.config.opening_price
            if self.config.opening_price is not None
            else round(self.config.seller_cost * 2.5, 2)
        )
        self.runtime = runtime_context or (runtime_manager.provision(self.config.model_dump(), scripted=dry_run) if runtime_manager else None)
        try:
            self._build_agents()
        except BaseException:
            if self.runtime:
                self.runtime.close()
            raise

    def _build_agents(self) -> None:
        cfg = self.config
        agents = cfg.agents
        for seat, role in enumerate(("seller", "buyer")):
            agent_cfg = agents[seat].model_dump() if len(agents) > seat else {"model": "gpt-4o-mini"}
            if agent_cfg.get("runtime", "in_process") != "in_process":
                if not self.runtime:
                    raise ValueError("buyer_seller remote seats require runtime context")
                from .remote import RemoteBargainingAgent
                agent = RemoteBargainingAgent(self.runtime, seat)
            elif self.dry_run:
                agent = (_ScriptedSeller(cfg.seller_cost, self.opening_price, cfg.max_rounds) if seat == 0
                         else _ScriptedBuyer(cfg.buyer_value, cfg.buyer_patience, cfg.max_rounds))
            else:
                common = {"num_items": cfg.num_items, "max_rounds": cfg.max_rounds,
                          "discount_factor": cfg.discount_factor}
                agent = (SellerAgent(make_llm_client(agent_cfg), cost=cfg.seller_cost,
                                     monotonic=cfg.enforce_monotonic_offers, **common) if seat == 0
                         else BuyerAgent(make_llm_client(agent_cfg), value=cfg.buyer_value, **common))
            setattr(self, role, agent)

    @classmethod
    def build_program(cls):
        from .workflow import build_program
        return build_program()

    @classmethod
    def runtime_capability(cls):
        from a2a_engine.workflow_runtime import RuntimeCapability
        from .remote import BUYER_SELLER_TOOLS
        return RuntimeCapability(BUYER_SELLER_TOOLS)

    # --- observations: the information-leakage boundary (§5) ---

    def _public_state(self, t: int, units_sold: int, history: list[dict]) -> dict[str, Any]:
        return {
            "round": t,
            "max_rounds": self.config.max_rounds,
            "rounds_remaining": self.config.max_rounds - t + 1,
            "units_sold": units_sold,
            "units_remaining": self.config.num_items - units_sold,
            "strike_prices": [h["price"] for h in history if h["accepted"]],
            "discount_factor": self.config.discount_factor,
            "history": list(history),
        }

    def _seller_observation(self, t, units_sold, history, last_price) -> dict[str, Any]:
        """Seller sees its own cost and the public state. Never ``buyer_value``."""
        obs = self._public_state(t, units_sold, history)
        obs["your_cost"] = self.config.seller_cost
        obs["your_last_offer"] = last_price
        obs["monotonic_required"] = self.config.enforce_monotonic_offers
        return obs

    def _buyer_observation(self, t, units_sold, history, price) -> dict[str, Any]:
        """Buyer sees its own value and the offer. Never ``seller_cost``."""
        obs = self._public_state(t, units_sold, history)
        obs["your_value"] = self.config.buyer_value
        obs["current_offer"] = price
        return obs

    # --- execution ---

    def run(self) -> EpisodeTrace:
        return asyncio.run(self._run_async())

    async def _run_async(self) -> EpisodeTrace:
        from .workflow import GameContext
        from .remote import RemoteBargainingAgent
        remote_agents = [agent for agent in (self.seller, self.buyer) if isinstance(agent, RemoteBargainingAgent)]
        succeeded = False
        try:
            state = await self.build_program().ainvoke({}, context=GameContext(self),
                config={"recursion_limit": self.config.max_rounds * 9 + 15})
            succeeded = True
            return self._trace(state)
        finally:
            if self.runtime:
                try:
                    if not succeeded:
                        for agent in remote_agents:
                            await asyncio.to_thread(agent.episode_end)
                finally:
                    self.runtime.close()

    def _trace(self, state):
        cfg = self.config
        units_sold, rounds_used = state["units_sold"], state["round"] - 1
        strikes, history = state["strikes"], state["history"]
        buyer_utility, seller_utility = state["buyer_utility"], state["seller_utility"]
        reason = SOLD_OUT if units_sold >= cfg.num_items else DEADLINE
        return EpisodeTrace(
            episode_uid=str(uuid.uuid4()),
            config=self.config,
            events=self.events.all(),
            final_state={
                "units_sold": units_sold,
                "rounds_used": rounds_used,
                "terminated_reason": reason,
                "strike_prices": [s["price"] for s in strikes],
                "trades": strikes,
                # Private values are part of the record even though neither agent
                # ever saw the other's — analysis needs both to score the outcome.
                "seller_cost": cfg.seller_cost,
                "buyer_value": cfg.buyer_value,
                "buyer_utility": round(buyer_utility, 6),
                "seller_utility": round(seller_utility, 6),
            },
            metrics=self._metrics(units_sold, rounds_used, strikes,
                                  buyer_utility, seller_utility, history),
        )

    def _metrics(self, units_sold, rounds_used, strikes,
                 buyer_utility, seller_utility, history) -> dict[str, Any]:
        cfg = self.config
        joint = buyer_utility + seller_utility
        prices = [s["price"] for s in strikes]
        offers = [h["price"] for h in history]

        # First-best under this protocol: every unit trades at the earliest round
        # it can, one per round. Not k*(v-c) — that would ignore the discounting
        # the protocol imposes and make efficiency unreachable by construction.
        gains = max(0.0, cfg.buyer_value - cfg.seller_cost)
        best_joint = best_joint_utility(
            cfg.seller_cost, cfg.buyer_value, cfg.num_items, cfg.discount_factor
        )

        return {
            "units_sold": units_sold,
            "sell_through": units_sold / cfg.num_items,
            "agreement": units_sold > 0,
            "rounds_used": rounds_used,
            "buyer_utility": round(buyer_utility, 6),
            "seller_utility": round(seller_utility, 6),
            "joint_utility": round(joint, 6),
            "best_joint_utility": round(best_joint, 6),
            "undiscounted_total_surplus": round(
                undiscounted_total_surplus(cfg.seller_cost, cfg.buyer_value, cfg.num_items), 6
            ),
            "efficiency": round(joint / best_joint, 6) if best_joint > 0 else None,
            "gains_from_trade": round(gains, 6),
            "buyer_share": round(buyer_utility / joint, 6) if joint > 0 else None,
            "mean_strike_price": round(sum(prices) / len(prices), 4) if prices else None,
            "first_offer": offers[0] if offers else None,
            "final_offer": offers[-1] if offers else None,
            "price_concession": round(offers[0] - offers[-1], 4) if len(offers) > 1 else 0.0,
            "monotonic_violations": sum(
                1 for e in self.events.all()
                if e.type == "offer" and e.data.get("monotonic_violation")
            ),
        }


register_environment(
    "buyer_seller",
    BuyerSellerGame,
    declaration=DECLARATION,
    package="buyer-seller",
    # Dry runs and smoke tests use the scripted agents above, so no keys needed.
    dry_run_checks_keys=False,
)
