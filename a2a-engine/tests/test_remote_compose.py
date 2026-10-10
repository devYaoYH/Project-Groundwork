import copy

import pytest

from a2a_engine.remote.compose import compose_enabled, runtime_config


def test_compose_is_opt_in_and_rosters_are_not_mutated(monkeypatch):
    config = {"agents": [{"type": "scripted", "id": "agent", "runtime": "in_process"}]}
    original = copy.deepcopy(config)
    monkeypatch.delenv("A2A_RUNTIME_MODE", raising=False)
    assert not compose_enabled()
    assert runtime_config(config) is config
    monkeypatch.setenv("A2A_RUNTIME_MODE", "compose")
    assert runtime_config(config)["agents"][0]["runtime"] == "external"
    assert config == original


@pytest.mark.parametrize("config", [{}, {"agents": [{"type": "human"}]},
                                    {"agents": [{"type": "llm", "api_key": "secret"}]}])
def test_compose_refuses_unscoped_or_secret_rosters(config, monkeypatch):
    monkeypatch.setenv("A2A_RUNTIME_MODE", "compose")
    with pytest.raises(ValueError):
        runtime_config(config)


def test_unknown_mode_does_not_silently_use_loopback(monkeypatch):
    monkeypatch.setenv("A2A_RUNTIME_MODE", "other")
    with pytest.raises(ValueError):
        compose_enabled()
