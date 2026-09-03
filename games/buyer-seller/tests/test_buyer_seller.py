"""Protocol tests for the buyer-seller environment.

These cover the rules that are easy to break silently in a refactor: the
information-leakage boundary, the discounting arithmetic, and the terminal
conditions. All run with scripted agents, so no API keys.
"""

from __future__ import annotations

import pytest

from buyer_seller.game import (
    DEADLINE,
    SOLD_OUT,
    BuyerSellerConfig,
    BuyerSellerGame,
    best_joint_utility,
    undiscounted_total_surplus,
)


def run(**overrides):
    cfg = {
        "num_items": 3, "seller_cost": 10.0, "buyer_value": 30.0,
        "max_rounds": 10, "discount_factor": 0.9, "seed": 1,
    }
    cfg.update(overrides)
    return BuyerSellerGame(BuyerSellerConfig(**cfg), dry_run=True).run()


def events_of(trace, kind):
    return [e for e in trace.events if e.type == kind]


# --- information asymmetry (SPEC §5) ---------------------------------------


def test_buyer_never_observes_seller_cost():
    """The leakage boundary is the spec's central constraint."""
    environment = BuyerSellerGame(BuyerSellerConfig(seller_cost=7.5, buyer_value=30.0), dry_run=True)
    obs = environment._buyer_observation(1, 0, [], price=20.0)
    assert "your_value" in obs
    assert "seller_cost" not in obs
    assert 7.5 not in [v for v in obs.values() if isinstance(v, (int, float))]


def test_seller_never_observes_buyer_value():
    environment = BuyerSellerGame(BuyerSellerConfig(seller_cost=10.0, buyer_value=33.25), dry_run=True)
    obs = environment._seller_observation(1, 0, [], last_price=None)
    assert "your_cost" in obs
    assert "buyer_value" not in obs
    assert 33.25 not in [v for v in obs.values() if isinstance(v, (int, float))]


# --- payoff mechanics (SPEC §4) --------------------------------------------


def test_utilities_match_discounted_surplus_formula():
    trace = run()
    delta = 0.9
    expected_buyer = sum(
        delta ** (tr["round"] - 1) * (30.0 - tr["price"])
        for tr in trace.final_state["trades"]
    )
    expected_seller = sum(
        delta ** (tr["round"] - 1) * (tr["price"] - 10.0)
        for tr in trace.final_state["trades"]
    )
    assert trace.metrics["buyer_utility"] == pytest.approx(expected_buyer, abs=1e-6)
    assert trace.metrics["seller_utility"] == pytest.approx(expected_seller, abs=1e-6)
    assert trace.metrics["joint_utility"] == pytest.approx(
        expected_buyer + expected_seller, abs=1e-6
    )


def test_later_trades_are_discounted_more():
    """delta must actually bite, otherwise the deadline carries no pressure."""
    trace = run(discount_factor=0.5)
    trades = trace.final_state["trades"]
    assert len(trades) >= 2
    discounts = [tr["discount"] for tr in trades]
    assert discounts == sorted(discounts, reverse=True)
    assert discounts[0] > discounts[-1]


def test_best_joint_utility_matches_the_item_tuple_discount_factor():
    """The configured-game optimum includes delta; total surplus is informational."""
    assert undiscounted_total_surplus(8.0, 30.0, 3) == 66.0
    assert best_joint_utility(8.0, 30.0, 3, 1.0) == 66.0
    assert best_joint_utility(8.0, 30.0, 3, 0.5) == pytest.approx(38.5)

    trace = run(seller_cost=8.0, buyer_value=30.0, num_items=3, discount_factor=0.5)
    assert trace.metrics["best_joint_utility"] == pytest.approx(38.5)
    assert trace.metrics["undiscounted_total_surplus"] == 66.0


def test_no_trade_yields_zero_utility():
    """SPEC §1 terminal rule: no agreement by T means zero for both sides."""
    trace = run(seller_cost=30.0, buyer_value=20.0)
    assert trace.metrics["units_sold"] == 0
    assert trace.metrics["agreement"] is False
    assert trace.metrics["buyer_utility"] == 0.0
    assert trace.metrics["seller_utility"] == 0.0
    assert trace.final_state["terminated_reason"] == DEADLINE


# --- terminal conditions (SPEC §3) -----------------------------------------


def test_stops_when_inventory_exhausted():
    trace = run(num_items=2)
    assert trace.metrics["units_sold"] == 2
    assert trace.final_state["terminated_reason"] == SOLD_OUT
    assert trace.metrics["sell_through"] == 1.0


def test_never_exceeds_deadline():
    trace = run(num_items=50, max_rounds=6)
    assert trace.metrics["rounds_used"] <= 6
    assert len(events_of(trace, "round_start")) <= 6
    assert trace.final_state["terminated_reason"] == DEADLINE


def test_buyer_never_accepts_above_its_value():
    """A trade above v is negative surplus; it would corrupt every efficiency stat."""
    trace = run(seller_cost=25.0, buyer_value=26.0, max_rounds=12)
    for tr in trace.final_state["trades"]:
        assert tr["price"] <= 26.0


# --- monotonicity (SPEC §5, optional) --------------------------------------


def test_monotonic_constraint_clamps_rising_offers():
    class RisingSeller:
        name = "seller"

        def __init__(self):
            self.n = 0

        async def act(self, observation, tools):
            self.n += 1
            return {"price": 10.0 * self.n, "text": f"offer {10.0 * self.n}"}

    environment = BuyerSellerGame(
        BuyerSellerConfig(num_items=3, seller_cost=1.0, buyer_value=5.0,
                          max_rounds=5, enforce_monotonic_offers=True),
        dry_run=True,
    )
    environment.seller = RisingSeller()
    trace = environment.run()

    prices = [e.data["price"] for e in events_of(trace, "offer")]
    assert prices == sorted(prices, reverse=True), "offers must be non-increasing"
    assert trace.metrics["monotonic_violations"] > 0


def test_violations_not_flagged_when_constraint_disabled():
    trace = run(enforce_monotonic_offers=False)
    assert trace.metrics["monotonic_violations"] == 0


# --- trace shape ------------------------------------------------------------


def test_offers_and_responses_are_transcript_visible():
    """Messages carry {speaker, text} so EpisodeDataset.to_messages_df works."""
    from a2a_engine.dataset import EpisodeDataset

    trace = run()
    ds = EpisodeDataset.from_traces([trace])
    df = ds.to_messages_df()
    assert not df.empty
    assert set(df["speaker"]) == {"seller", "buyer"}


def test_run_is_deterministic_for_a_fixed_config():
    """Reproducibility: same config in, same trajectory out."""
    a, b = run(seed=7), run(seed=7)
    assert [e.data.get("price") for e in events_of(a, "offer")] == \
           [e.data.get("price") for e in events_of(b, "offer")]
    assert a.metrics == b.metrics


def test_strike_prices_are_locked_once_agreed():
    """SPEC §5: a completed transaction cannot be renegotiated."""
    trace = run()
    trades = trace.final_state["trades"]
    assert [t["unit_index"] for t in trades] == list(range(1, len(trades) + 1))
    assert trace.final_state["strike_prices"] == [t["price"] for t in trades]
