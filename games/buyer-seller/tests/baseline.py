"""Test-only frozen bargaining loop from e1286f6; never used in production."""

from a2a_engine import EpisodeTrace
from buyer_seller.game import DEADLINE, SOLD_OUT


async def original_loop(game):
    cfg = game.config
    game.events.append("game_start", data={
        "num_items": cfg.num_items, "max_rounds": cfg.max_rounds,
        "discount_factor": cfg.discount_factor,
        "enforce_monotonic_offers": cfg.enforce_monotonic_offers,
        "opening_price": game.opening_price,
    })
    units_sold, history, strikes = 0, [], []
    buyer_utility, seller_utility = 0.0, 0.0
    last_price, t, reason = None, 1, DEADLINE
    while units_sold < cfg.num_items and t <= cfg.max_rounds:
        game.events.append("round_start", data={"round": t, "units_remaining": cfg.num_items - units_sold})
        action = await game.seller.act(game._seller_observation(t, units_sold, history, last_price), tools={})
        price, violation = float(action["price"]), False
        if cfg.enforce_monotonic_offers and last_price is not None and price > last_price:
            violation, price = True, last_price
        price = round(price, 2)
        game.events.append("offer", data={"round": t, "speaker": "seller",
            "text": action.get("text") or f"I offer one unit at {price:.2f}.",
            "price": price, "monotonic_violation": violation})
        response = await game.buyer.act(game._buyer_observation(t, units_sold, history, price), tools={})
        accepted = bool(response["accept"])
        game.events.append("response", data={"round": t, "speaker": "buyer",
            "text": response.get("text") or ("I accept." if accepted else "I reject."),
            "decision": "accept" if accepted else "reject", "price": price})
        if accepted:
            discount = cfg.discount_factor ** (t - 1)
            buyer_surplus, seller_surplus = discount * (cfg.buyer_value - price), discount * (price - cfg.seller_cost)
            buyer_utility += buyer_surplus
            seller_utility += seller_surplus
            units_sold += 1
            strikes.append({"unit_index": units_sold, "round": t, "price": price, "discount": discount,
                            "buyer_surplus": buyer_surplus, "seller_surplus": seller_surplus})
            game.events.append("trade", data=strikes[-1])
        history.append({"round": t, "price": price, "accepted": accepted})
        last_price, t = price, t + 1
        if units_sold >= cfg.num_items:
            reason = SOLD_OUT
    rounds_used = t - 1
    game.events.append("game_end", data={"reason": reason, "units_sold": units_sold, "rounds_used": rounds_used})
    return EpisodeTrace(episode_uid="baseline", config=cfg, events=game.events.all(), final_state={
        "units_sold": units_sold, "rounds_used": rounds_used, "terminated_reason": reason,
        "strike_prices": [s["price"] for s in strikes], "trades": strikes,
        "seller_cost": cfg.seller_cost, "buyer_value": cfg.buyer_value,
        "buyer_utility": round(buyer_utility, 6), "seller_utility": round(seller_utility, 6),
    }, metrics=game._metrics(units_sold, rounds_used, strikes, buyer_utility, seller_utility, history))
