"""LLM agents for the buyer-seller bargaining game.

Each agent's system prompt states only what that side is entitled to know: the
seller is told its cost and that the buyer's valuation is unknown, and the buyer
the reverse. The prompts are the second line of defence — the game's observation
builders are the first, and never hand over the other side's private value.

Both parsers are deliberately forgiving. A bargaining trajectory is expensive to
produce, and discarding one because a model wrote "$14.50" instead of
"PRICE: 14.50" would throw away signal over syntax. Unparseable output falls
back to a defined default and is flagged in the returned action so analysis can
filter those rounds.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from a2a_engine import LLMAgent

SELLER_SYSTEM = """\
You are the SELLER in a multi-round bargaining game.

Your private information:
- Your cost per unit is {cost:.2f}. Selling below this loses you money.
- You have {num_items} identical units to sell, one per round at most.
- You do NOT know the buyer's valuation. Infer it from what they accept and reject.

Rules:
- The game lasts at most {max_rounds} rounds. Each round you name a price and the
  buyer accepts or rejects.
- Payoffs are discounted by {discount:.2f} per round elapsed, so a deal now is
  worth more than the same deal later.
- If nothing is sold by the deadline, you get zero.
{monotonic_rule}
Respond with exactly one line:
PRICE: <number>
Optionally add one short sentence of persuasion after the price line.\
"""

MONOTONIC_RULE = (
    "- Your offers must never increase: each price must be <= your previous price.\n"
)

BUYER_SYSTEM = """\
You are the BUYER in a multi-round bargaining game.

Your private information:
- Each unit is worth {value:.2f} to you. Paying more than this loses you money.
- You want up to {num_items} units, at most one per round.
- You do NOT know the seller's cost. Infer it from how their offers move.

Rules:
- The game lasts at most {max_rounds} rounds. Each round the seller names a price
  and you accept or reject.
- Payoffs are discounted by {discount:.2f} per round elapsed, so holding out for a
  better price costs you value. Rejecting to the deadline earns you zero.

Respond with exactly one line:
DECISION: ACCEPT
or
DECISION: REJECT
Optionally add one short sentence of reasoning after the decision line.\
"""

_PRICE_RE = re.compile(r"PRICE:\s*\$?\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_ANY_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
_ACCEPT_RE = re.compile(r"\bACCEPT\b", re.IGNORECASE)
_REJECT_RE = re.compile(r"\bREJECT\b", re.IGNORECASE)


def _history_lines(observation: dict[str, Any]) -> list[str]:
    lines = []
    for turn in observation.get("history", []):
        outcome = "ACCEPTED" if turn["accepted"] else "rejected"
        lines.append(f"  round {turn['round']}: offered {turn['price']:.2f} -> {outcome}")
    return lines


class SellerAgent(LLMAgent):
    """Names a price each round."""

    def __init__(self, client, *, cost: float, num_items: int, max_rounds: int,
                 discount_factor: float, monotonic: bool = False) -> None:
        super().__init__(
            client,
            system_prompt=SELLER_SYSTEM.format(
                cost=cost, num_items=num_items, max_rounds=max_rounds,
                discount=discount_factor,
                monotonic_rule=MONOTONIC_RULE if monotonic else "",
            ),
            name="seller",
        )
        self.cost = cost

    def build_messages(self, observation: dict[str, Any],
                       tools: dict[str, Callable]) -> list[dict]:
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": self.build_prompt(observation)},
        ]

    def build_prompt(self, observation: dict[str, Any]) -> str:
        lines = [
            f"Round {observation['round']} of {observation['max_rounds']}.",
            f"Units sold so far: {observation['units_sold']}; "
            f"remaining: {observation['units_remaining']}.",
        ]
        history = _history_lines(observation)
        if history:
            lines.append("Offer history:")
            lines.extend(history)
        if observation.get("your_last_offer") is not None:
            lines.append(f"Your previous offer: {observation['your_last_offer']:.2f}")
        lines.append("Name your price for this round.")
        return "\n".join(lines)

    def parse_response(self, text: str, observation, tools) -> dict[str, Any]:
        match = _PRICE_RE.search(text or "")
        if match is None:
            # Fall back to the first number anywhere in the reply before giving
            # up on the round entirely.
            match = _ANY_NUMBER_RE.search(text or "")
        if match is None:
            # No number at all: repeat the last offer, or open at cost. Holding
            # the previous price is the neutral move — it neither concedes nor
            # escalates on the model's behalf.
            fallback = observation.get("your_last_offer")
            price = float(fallback) if fallback is not None else float(self.cost)
            return {"price": price, "text": (text or "").strip()[:280],
                    "parse_failed": True}
        return {"price": float(match.group(1)), "text": (text or "").strip()[:280],
                "parse_failed": False}


class BuyerAgent(LLMAgent):
    """Accepts or rejects the standing offer."""

    def __init__(self, client, *, value: float, num_items: int, max_rounds: int,
                 discount_factor: float) -> None:
        super().__init__(
            client,
            system_prompt=BUYER_SYSTEM.format(
                value=value, num_items=num_items, max_rounds=max_rounds,
                discount=discount_factor,
            ),
            name="buyer",
        )
        self.value = value

    def build_messages(self, observation: dict[str, Any],
                       tools: dict[str, Callable]) -> list[dict]:
        return [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": self.build_prompt(observation)},
        ]

    def build_prompt(self, observation: dict[str, Any]) -> str:
        lines = [
            f"Round {observation['round']} of {observation['max_rounds']} "
            f"({observation['rounds_remaining']} remaining).",
            f"Units bought so far: {observation['units_sold']}; "
            f"you still want {observation['units_remaining']}.",
        ]
        history = _history_lines(observation)
        if history:
            lines.append("Offer history:")
            lines.extend(history)
        lines.append(f"The seller now offers one unit at {observation['current_offer']:.2f}.")
        lines.append("Accept or reject.")
        return "\n".join(lines)

    def parse_response(self, text: str, observation, tools) -> dict[str, Any]:
        body = text or ""
        accept = bool(_ACCEPT_RE.search(body))
        reject = bool(_REJECT_RE.search(body))
        if accept == reject:
            # Ambiguous or empty: reject, which is the status-quo action. It
            # keeps the game running and never commits the buyer to a trade the
            # model did not clearly ask for.
            return {"accept": False, "text": body.strip()[:280], "parse_failed": True}
        return {"accept": accept, "text": body.strip()[:280], "parse_failed": False}
