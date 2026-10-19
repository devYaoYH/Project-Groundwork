"""Seam tests: logical agent names -> concrete provider bindings.

The pool exists because the same logical agent reaches a different endpoint for
different researchers. The risk is that a binding silently changes what ran, so
hydration is pinned here alongside the guardrails that refuse ambiguous configs.
"""

import pytest

from a2a_engine.agent_pool import (
    AgentPool, AgentPoolEntry, hydrate_participants, load_agent_pool,
)


def write_pool(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def test_entry_drops_pool_only_fields_from_the_game_config():
    """`description` and `credential` describe the binding, not the agent."""
    entry = AgentPoolEntry(
        description="notes", model="gpt-5-mini", api_format="openai",
        credential="OPENAI_API_KEY", config={"reasoning": "low"},
    )

    config = entry.as_agent_config()

    assert config == {"type": "llm", "model": "gpt-5-mini",
                      "api_format": "openai", "reasoning": "low"}


def test_local_bindings_shadow_shared_defaults_by_name(tmp_path, monkeypatch):
    """Two researchers, one logical name, different routes."""
    monkeypatch.delenv("A2A_AGENT_POOL", raising=False)
    write_pool(tmp_path / "experiments/agents.yaml", """
agents:
  haiku:
    model: claude-haiku-4-5-20251001
    api_format: anthropic
    credential: ANTHROPIC_API_KEY
  gpt-mini:
    model: gpt-5-mini
    api_format: openai
    credential: OPENAI_API_KEY
""")
    write_pool(tmp_path / "agents.local.yaml", """
agents:
  haiku:
    model: anthropic/claude-haiku-4.5
    api_format: openai
    api_base: https://openrouter.ai/api/v1
    credential: OPENROUTER_API_KEY
""")
    pool = load_agent_pool(tmp_path / "experiments/some_experiment.yaml")

    assert pool.entry("haiku").model == "anthropic/claude-haiku-4.5"
    assert pool.entry("haiku").credential == "OPENROUTER_API_KEY"
    # An entry the override does not mention is untouched.
    assert pool.entry("gpt-mini").model == "gpt-5-mini"


def test_missing_credentials_are_reported_by_variable_name(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    pool = AgentPool(agents={
        "gpt-mini": AgentPoolEntry(model="gpt-5-mini", credential="OPENAI_API_KEY"),
        "haiku": AgentPoolEntry(model="haiku", credential="ANTHROPIC_API_KEY"),
        "heuristic": AgentPoolEntry(type="heuristic"),
    })

    assert pool.missing_credentials(["gpt-mini", "haiku", "heuristic"]) == ["OPENAI_API_KEY"]


def test_hydration_is_positional_and_repeats_are_independent_seats():
    pool = AgentPool(agents={"a": AgentPoolEntry(model="m-a"), "b": AgentPoolEntry(model="m-b")})

    assert [entry["model"] for entry in hydrate_participants(["a", "b", "a"], pool)] == \
        ["m-a", "m-b", "m-a"]


def test_an_unknown_name_names_what_the_pool_does_define():
    pool = AgentPool(agents={"gpt-mini": AgentPoolEntry(model="gpt-5-mini")})

    with pytest.raises(KeyError, match="unknown agent 'nope'"):
        hydrate_participants(["nope"], pool)


def test_the_shipped_pool_covers_the_credentials_env_example_documents():
    """A researcher who fills in .env should be able to run the worked example."""
    from pathlib import Path

    workspace = Path(__file__).resolve().parents[2]
    pool = load_agent_pool(workspace / "experiments")

    assert {"gpt-mini", "haiku"} <= set(pool.agents)
    needed = {pool.entry(name).credential for name in ("gpt-mini", "haiku")}
    documented = (workspace / ".env.example").read_text()
    for variable in needed:
        assert variable in documented, f"{variable} is not documented in .env.example"


def test_openrouter_entries_carry_the_routing_a_client_needs():
    """An OpenRouter binding reaches a different host with the OpenAI wire
    format, so both have to travel with the entry rather than be guessed."""
    from pathlib import Path

    workspace = Path(__file__).resolve().parents[2]
    pool = load_agent_pool(workspace / "experiments")

    for name in ("ds-flash", "haiku-or"):
        entry = pool.entry(name)
        assert entry.api_base == "https://openrouter.ai/api/v1"
        assert entry.api_format == "openai"
        assert entry.credential == "OPENROUTER_API_KEY"
        assert entry.as_agent_config()["api_base"] == "https://openrouter.ai/api/v1"


def test_shipped_deepseek_41_binding_admits_image_seats():
    from pathlib import Path
    from a2a_engine.modalities import validate_seat
    workspace = Path(__file__).resolve().parents[2]
    entry = load_agent_pool(workspace / "experiments").entry("ds-v4.1-flash")
    config = entry.as_agent_config()
    assert config["model"] == "deepseek/deepseek-v4.1-flash"
    assert config["api_base"] == "https://openrouter.ai/api/v1"
    assert config["api_format"] == "openai"
    validate_seat(config, {"text", "image"})
    assert entry.credential == "OPENROUTER_API_KEY"


def test_one_logical_agent_can_be_reached_by_two_routes():
    """`haiku` and `haiku-or` are the same model on different paths — the whole
    reason the pool is a layer rather than a lookup."""
    from pathlib import Path

    workspace = Path(__file__).resolve().parents[2]
    pool = load_agent_pool(workspace / "experiments")

    direct, routed = pool.entry("haiku"), pool.entry("haiku-or")

    assert direct.api_format == "anthropic" and routed.api_format == "openai"
    assert direct.credential != routed.credential
    assert direct.api_base is None and routed.api_base


def test_runtime_harness_survive_pool_hydration_and_external_needs_no_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    pool = AgentPool(agents={
        "external": AgentPoolEntry(runtime="external", type="llm", credential="OPENAI_API_KEY"),
        "local": AgentPoolEntry(runtime="local_process", type="scripted", harness="scripted"),
        "flattened": AgentPoolEntry(credential="OPENAI_API_KEY", config={"runtime": "external"}),
    })
    hydrated = hydrate_participants(["external", "local", "flattened"], pool)
    assert [spec["runtime"] for spec in hydrated] == ["external", "local_process", "external"]
    assert hydrated[1]["harness"] == "scripted"
    assert pool.missing_credentials(["external", "flattened"]) == []
    hydrated[1]["harness"] = "changed"
    assert pool.entry("local").harness == "scripted"
    with pytest.raises(ValueError):
        AgentPoolEntry(config={"runtime": "unknown"}).as_agent_config()


@pytest.mark.parametrize("family", ["haiku", "sonnet", "opus"])
@pytest.mark.parametrize("auth", ["api-key", "oauth"])
def test_claude_55_participants_reach_anthropic_without_temperature(
    family, auth, tmp_path, monkeypatch,
):
    """Exercise CLI hydration through the actual request builder, without HTTP."""
    import io
    import json
    from pathlib import Path

    from a2a_engine.experiment import load_experiment
    from a2a_engine.llm import api
    from a2a_engine.llm.factory import make_llm_client
    from expt_runner.run_experiment import _check_api_keys

    workspace = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("A2A_AGENT_POOL", str(workspace / "experiments/agents.yaml"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    variable, key = ("ANTHROPIC_API_KEY", "test-api-key") if auth == "api-key" else (
        "ANTHROPIC_AUTH_TOKEN", "sk-ant-oat-test-token",
    )
    monkeypatch.setenv(variable, key)
    experiment = tmp_path / "claude.yaml"
    experiment.write_text(
        f"name: claude_55\nparticipants: [{family}-5.5, {family}-5.5]\n"
        "defaults: {environment_id: word_guess, num_agents: 2}\n"
        "cells: [{label: baseline, count: 1}]\n"
    )
    config = load_experiment(experiment).defaults
    _check_api_keys(config, mode="live")
    assert [agent["model"] for agent in config["agents"]] == [f"claude-{family}-5-5"] * 2
    assert all("credential" not in agent and "api_key" not in agent for agent in config["agents"])

    def fake_urlopen(request, **kwargs):
        assert request.full_url == "https://api.anthropic.com/v1/messages"
        body = json.loads(request.data)
        assert body["model"] == f"claude-{family}-5-5"
        assert "temperature" not in body
        assert body["max_tokens"] > 0
        headers = {name.lower(): value for name, value in request.header_items()}
        if auth == "oauth":
            assert headers["authorization"] == f"Bearer {key}"
            assert headers["anthropic-beta"] == "oauth-2025-04-20"
            assert "x-api-key" not in headers
        else:
            assert headers["x-api-key"] == key
            assert "authorization" not in headers
        return io.BytesIO(b'{"content": [{"type": "text", "text": "ok"}]}')

    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)
    client = make_llm_client(config["agents"][0])
    assert client.oneshot([{"role": "user", "content": "test"}]) == "ok"
    # Existing studies retain their Haiku 4.5 endpoint.
    assert load_agent_pool(workspace / "experiments").entry("haiku").model == "claude-haiku-4-5-20251001"
