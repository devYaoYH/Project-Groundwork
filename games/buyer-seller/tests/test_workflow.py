import asyncio
from copy import deepcopy
from functools import wraps

import pytest

from a2a_engine.workflow import export_program
from buyer_seller.agents import BuyerAgent, SellerAgent
from buyer_seller.game import BuyerSellerGame
from .baseline import original_loop


class ControlledAgent:
    def __init__(self, role, actions, calls):
        self.role, self.actions, self.calls, self.index = role, actions, calls, 0

    async def act(self, observation, tools):
        self.calls.append((self.role, deepcopy(observation)))
        action = self.actions[min(self.index, len(self.actions) - 1)]
        self.index += 1
        return deepcopy(action)


class MockProvider:
    def __init__(self, lines):
        self.lines, self.messages = iter(lines), []

    def oneshot(self, messages):
        self.messages.append(deepcopy(messages))
        return next(self.lines)


CASES = [
    ({"num_items": 2, "max_rounds": 5, "discount_factor": 0.5, "enforce_monotonic_offers": True},
     [{"price": 20.126, "text": "opening"}, {"price": 30.0}, {"price": 12.125, "text": ""}],
     [{"accept": False}, {"accept": True}, {"accept": True, "text": "yes"}]),
    ({"num_items": 4, "max_rounds": 3}, [{"price": 25}], [{"accept": False}]),
    ({"num_items": 4, "max_rounds": 3}, [{"price": 20}], [{"accept": True}]),
    ({"num_items": 50, "max_rounds": 40}, [{"price": 20}], [{"accept": True}]),
    ({"num_items": 1, "max_rounds": 1}, [{"price": -2.345}], [{"accept": True}]),
    ({"max_rounds": 40, "enforce_monotonic_offers": False},
     [{"price": 5}, {"price": 999}], [{"accept": False}]),
    ({"seller_cost": 30, "buyer_value": 20, "max_rounds": 2},
     [{"price": 100}], [{"accept": True}]),
]


def controlled(config, offers, responses):
    game = BuyerSellerGame(config, dry_run=True)
    calls = []
    game.seller = ControlledAgent("seller", offers, calls)
    game.buyer = ControlledAgent("buyer", responses, calls)
    return game, calls


def assert_trace_parity(actual, expected):
    assert [(e.type, e.data) for e in actual.events] == [(e.type, e.data) for e in expected.events]
    assert actual.final_state == expected.final_state
    assert actual.metrics == expected.metrics


@pytest.mark.parametrize("config,offers,responses", CASES)
def test_frozen_loop_exact_observation_events_and_results(config, offers, responses):
    old, old_calls = controlled(config, offers, responses)
    expected = asyncio.run(original_loop(old))
    game, calls = controlled(config, offers, responses)
    assert_trace_parity(game.run(), expected)
    assert calls == old_calls
    for role, obs in calls:
        assert ("your_cost" in obs) == (role == "seller")
        assert ("your_value" in obs) == (role == "buyer")
        assert len(obs["history"]) == obs["round"] - 1
    if config.get("enforce_monotonic_offers"):
        assert calls[3][1]["current_offer"] == 20.13
        assert calls[4][1]["your_last_offer"] == 20.13
        assert calls[4][1]["units_sold"] == 1


def test_export_is_deterministic_and_never_executes_domain_or_runtime(monkeypatch):
    from buyer_seller import workflow, game, remote, runtime_strategy
    def fail(*args, **kwargs):
        raise AssertionError("export executed game code")
    for module, names in ((workflow, ["initialize", "can_continue", "accepted_offer", "seller_observation",
                                      "buyer_observation", "select_seller", "select_buyer", "registration",
                                      "trade_one_unit", "normalize_offer", "lifecycle_adapter", "bargaining_adapter",
                                      "seller_registration", "buyer_registration", "no_observation",
                                      "inventory_deadline_guard", "start_round", "emit_offer", "emit_response",
                                      "advance_round", "finalize"]),
                          (game.BuyerSellerGame, ["__init__", "_build_agents", "_seller_observation", "_buyer_observation"]),
                          (game, ["make_llm_client"]), (remote.RemoteBargainingAgent, ["__init__"]),
                          (runtime_strategy.BuyerSellerStrategy, ["__init__"]),
                          (remote.TurnDispatcherV2, ["__init__"]),
                          (game.EventLog, ["from_config", "append"])):
        for name in names:
            monkeypatch.setattr(module, name, wraps(getattr(module, name))(lambda *args, **kwargs: fail()))
    first = export_program(BuyerSellerGame.build_program(), "buyer_seller")
    assert first == export_program(BuyerSellerGame.build_program(), "buyer_seller")
    assert first["status"] == "available"
    graph = first["workflow"]["graphs"][0]
    assert {e.get("route_key") for e in graph["edges"] if e["conditional"]} == {"continue", "finished", "accepted", "rejected"}
    assert len(graph["nodes"]) == 17
    assert len(graph["edges"]) == 18
    assert {node["actor_selector"] for node in graph["nodes"] if node["kind"] == "agent_call"} == {
        "buyer_seller.workflow.select_seller", "buyer_seller.workflow.select_buyer"}


def test_agent_gateway_rejects_hidden_calls():
    from buyer_seller.workflow import GameContext
    game = BuyerSellerGame({}, dry_run=True)
    GameContext(game)
    with pytest.raises(RuntimeError, match="executable AgentStep"):
        asyncio.run(game.seller.act({}, {}))


@pytest.mark.parametrize("seller_lines,buyer_lines", [
    (["PRICE: $20.126", "PRICE: 40", "no number"], ["REJECT", "ACCEPT", "ACCEPT"]),
    (["", "PRICE: $-1.23", "unparseable"], ["", "ACCEPT and REJECT", "ACCEPT"]),
])
def test_provider_prompts_and_parser_oracle(seller_lines, buyer_lines):
    config = {"num_items": 3, "max_rounds": 3, "enforce_monotonic_offers": True}
    traces, messages = [], []
    for baseline in (True, False):
        game = BuyerSellerGame(config, dry_run=True)
        seller, buyer = MockProvider(seller_lines), MockProvider(buyer_lines)
        game.seller = SellerAgent(seller, cost=10, num_items=3, max_rounds=3, discount_factor=.9, monotonic=True)
        game.buyer = BuyerAgent(buyer, value=30, num_items=3, max_rounds=3, discount_factor=.9)
        traces.append(asyncio.run(original_loop(game)) if baseline else game.run())
        messages.append((seller.messages, buyer.messages))
    assert_trace_parity(*traces)
    assert messages[0] == messages[1]


def test_bare_numeric_seller_reply_preserves_existing_parser_exception():
    for baseline in (True, False):
        game = BuyerSellerGame({"max_rounds": 1}, dry_run=True)
        game.seller = SellerAgent(MockProvider(["ask $-1.23"]), cost=10, num_items=3,
                                  max_rounds=1, discount_factor=.9)
        with pytest.raises(IndexError, match="no such group"):
            asyncio.run(original_loop(game)) if baseline else game.run()
        assert [e.type for e in game.events.all()] == ["game_start", "round_start"]
