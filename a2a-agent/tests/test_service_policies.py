from types import SimpleNamespace

import pytest
from a2a_agent.seat_plugins import seat_plugins
from a2a_agent.service import descriptor_policy
from a2a_engine.remote.network import NetworkPolicy


def test_legacy_controlled_service_keeps_its_policy_hook(tmp_path, monkeypatch):
    from a2a_agent import service

    selected = []
    monkeypatch.setattr(
        service, "ProbePolicy", lambda directory, network: selected.append(directory)
    )
    descriptor_policy({"environment_id": "calendar"}, tmp_path, NetworkPolicy())
    assert selected == [tmp_path]


@pytest.mark.parametrize("game", sorted(seat_plugins()))
def test_descriptor_strategies_are_constructed_without_provider_calls(game, tmp_path):
    policy = descriptor_policy(
        {"environment_id": game, "policy": {"type": "scripted"}}, tmp_path, NetworkPolicy()
    )
    assert callable(policy.calls)


@pytest.mark.parametrize("game", ["word_guess", "buyer_seller", "negotiation"])
def test_legacy_descriptor_keeps_game_specific_strategy(game, tmp_path):
    policy = descriptor_policy({"environment_id": game}, tmp_path, NetworkPolicy())
    assert type(policy).__name__ in {
        "WordGuessStrategy",
        "BuyerSellerStrategy",
        "NegotiationStrategy",
    }


@pytest.mark.parametrize("game", ["calendar", "word_guess", "buyer_seller"])
def test_provider_construction_is_agent_side_and_retains_model_configuration(
    game, tmp_path, monkeypatch
):
    calls = []
    client = SimpleNamespace()

    def factory(config):
        calls.append(config)
        return client

    monkeypatch.setattr("a2a_engine.llm.factory.make_llm_client", factory)
    descriptor = {
        "environment_id": game,
        "policy": {
            "type": "llm",
            "model": "deepseek/deepseek-v4-flash",
            "api_format": "openai",
            "temperature": 0.0,
            "max_tokens": 4096,
        },
    }
    policy = descriptor_policy(descriptor, tmp_path, NetworkPolicy())
    assert policy.client is client if game != "calendar" else policy.model is client
    assert calls == [{key: value for key, value in descriptor["policy"].items() if key != "type"}]


@pytest.mark.parametrize(
    "config", [{"api_key": "never-in-descriptors"}, {"type": "human"}, {"type": "heuristic"}]
)
def test_descriptor_rejects_credentials_and_unsupported_strategies(config, tmp_path):
    with pytest.raises(ValueError):
        descriptor_policy(
            {"environment_id": "calendar", "policy": config}, tmp_path, NetworkPolicy()
        )


def test_descriptor_retains_its_narrower_option_allowlist(tmp_path):
    with pytest.raises(ValueError, match="unsupported agent policy"):
        descriptor_policy(
            {
                "environment_id": "calendar",
                "policy": {"type": "llm", "model": "gpt-test", "extra": {}},
            },
            tmp_path,
            NetworkPolicy(),
        )
