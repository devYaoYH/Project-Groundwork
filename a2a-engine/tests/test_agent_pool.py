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
