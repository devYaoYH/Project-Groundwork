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


def test_haiku_55_participants_use_vertex_adc_without_an_anthropic_key(tmp_path, monkeypatch):
    """CLI hydration must reach Vertex with ADC and omit unsupported sampling."""
    import io
    import json
    from pathlib import Path
    from types import SimpleNamespace

    from a2a_engine.experiment import load_experiment
    from a2a_engine.llm import api
    from a2a_engine.llm.factory import detect_provider, make_llm_client
    from expt_runner.run_experiment import _check_api_keys

    workspace = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("A2A_AGENT_POOL", str(workspace / "experiments/agents.yaml"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    experiment = tmp_path / "haiku.yaml"
    experiment.write_text(
        "name: haiku_55\nparticipants: [haiku-5.5-vertex, haiku-5.5-vertex]\n"
        "defaults: {environment_id: word_guess, num_agents: 2}\n"
        "cells: [{label: baseline, count: 1}]\n"
    )
    config = load_experiment(experiment).defaults
    _check_api_keys(config, mode="live")
    assert [agent["model"] for agent in config["agents"]] == ["claude-haiku-5-5"] * 2
    assert all("credential" not in agent and "api_key" not in agent for agent in config["agents"])

    credentials = SimpleNamespace(token="test-adc-token", refresh=lambda request: None)
    monkeypatch.setattr(api, "_load_vertex_credentials", lambda path: (credentials, "test-project"))
    def fake_urlopen(request, **kwargs):
        assert request.full_url == (
            "https://aiplatform.googleapis.com/v1/projects/test-project/locations/global/"
            "publishers/anthropic/models/claude-haiku-5-5:rawPredict"
        )
        body = json.loads(request.data)
        assert body["anthropic_version"] == "vertex-2023-10-16"
        assert "temperature" not in body
        assert body["max_tokens"] > 0
        headers = {name.lower(): value for name, value in request.header_items()}
        assert headers["authorization"] == "Bearer test-adc-token"
        assert "x-api-key" not in headers
        return io.BytesIO(b'{"content": [{"type": "text", "text": "ok"}]}')

    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)
    client = make_llm_client(config["agents"][0])
    assert client.api_key == "" and client.api_base == ""
    assert client.oneshot([{"role": "user", "content": "test"}]) == "ok"
    pool = load_agent_pool(workspace / "experiments")
    assert pool.entry("haiku-5.5-vertex").credential is None
    assert pool.entry("haiku-5.5-vertex").source == "vertexai"
    assert "haiku-5.5" not in pool.agents
    assert not {"sonnet-5.5", "opus-5.5"} & pool.agents.keys()
    assert pool.entry("haiku").model == "claude-haiku-4-5-20251001"
    # An inline direct Anthropic configuration keeps its existing route.
    assert detect_provider("claude-haiku-5-5") == "anthropic"
    assert make_llm_client({"model": "claude-haiku-5-5"}).api_format == "anthropic"


def test_same_haiku_model_can_bind_to_independent_sources_and_accounts(monkeypatch):
    from a2a_engine.llm.factory import make_llm_client
    from a2a_engine.manifest import EpisodeManifest
    from a2a_engine.provenance import pinned_participants
    from expt_runner.run_experiment import _check_api_keys

    monkeypatch.setenv("ANTHROPIC_API_KEY", "default-account-key")
    monkeypatch.setenv("ANTHROPIC_RESEARCH_API_KEY", "research-account-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat-unrelated")
    pool = AgentPool(agents={
        "haiku-research": AgentPoolEntry(model="claude-haiku-5-5", source="anthropic",
            credential="ANTHROPIC_RESEARCH_API_KEY"),
        "haiku-vertex": AgentPoolEntry(model="claude-haiku-5-5", source="vertexai",
            api_format="vertexai_anthropic", config={"gcp_project": "study-project", "gcp_location": "global"}),
    })
    direct, vertex = hydrate_participants(["haiku-research", "haiku-vertex"], pool)
    assert direct["model"] == vertex["model"]
    assert direct["credential"] == "ANTHROPIC_RESEARCH_API_KEY"
    direct_client, vertex_client = make_llm_client(direct), make_llm_client(vertex)
    assert direct_client.api_key == "research-account-key"
    assert direct_client.api_format == "anthropic"
    assert vertex_client.api_key == "" and vertex_client.api_format == "vertexai_anthropic"
    assert vertex_client.gcp_project == "study-project"
    assert pinned_participants({"agents": [direct]})[0]["config_sha256"] != pinned_participants({"agents": [vertex]})[0]["config_sha256"]
    manifest = EpisodeManifest.from_run(config={"agents": [direct, vertex]}, experiment_name="source-test", cell_id="one", episode_idx=0, episode_uid="test")
    assert [agent.model_dump()["source"] for agent in manifest.agents] == ["anthropic", "vertexai"]
    monkeypatch.delenv("ANTHROPIC_RESEARCH_API_KEY")
    # An available key/token for a different account must not be substituted.
    assert make_llm_client(direct).api_key == ""
    with pytest.raises(EnvironmentError, match="ANTHROPIC_RESEARCH_API_KEY"):
        _check_api_keys({"agents": [direct]}, mode="live")
    _check_api_keys({"agents": [vertex]}, mode="live")


@pytest.mark.parametrize("config", [
    {"source": "vertexai"},
    {"source": "vertexai", "api_format": "anthropic"},
    {"source": "vertexai", "api_format": "vertexai_anthropic", "credential": "ANTHROPIC_API_KEY"},
    {"source": "anthropic", "api_format": "vertexai_anthropic"},
    {"source": "anthropic", "credential": "A2A_JOIN_TICKET"},
    {"source": "openrouter", "credential": "OPENAI_API_KEY"},
    {"source": "nonexistent"},
])
def test_explicit_source_refuses_conflicts_or_unrelated_credentials(config):
    from a2a_engine.llm.factory import make_llm_client
    with pytest.raises(ValueError):
        make_llm_client({"model": "claude-haiku-5-5", **config})


def test_explicit_source_materializes_the_default_account_reference(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    entry = AgentPoolEntry(model="claude-haiku-5-5", source="anthropic")
    assert entry.as_agent_config()["credential"] == "ANTHROPIC_API_KEY"
    assert AgentPool(agents={"direct": entry}).missing_credentials(["direct"]) == ["ANTHROPIC_API_KEY"]
