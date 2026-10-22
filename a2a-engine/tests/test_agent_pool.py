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
    """Descriptions stay pool-only; the access method is frozen into the plan."""
    entry = AgentPoolEntry(
        description="notes", model="gpt-5-mini", api_format="openai",
        credential="OPENAI_API_KEY", config={"reasoning": "low"},
    )

    config = entry.as_agent_config()

    assert config == {"type": "llm", "model": "gpt-5-mini",
                      "api_format": "openai", "reasoning": "low", "credential": "OPENAI_API_KEY"}


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

    for name in ("ds-flash", "haiku", "haiku-or"):
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


def test_both_shared_haiku_names_use_the_replacement_openrouter_model():
    """Existing participant names pick up the replacement when resolved anew."""
    from pathlib import Path

    workspace = Path(__file__).resolve().parents[2]
    pool = load_agent_pool(workspace / "experiments")

    direct, routed = pool.entry("haiku"), pool.entry("haiku-or")

    assert direct.as_agent_config() == routed.as_agent_config()
    assert direct.model == "anthropic/claude-haiku-5.5"
    assert direct.credential == "OPENROUTER_API_KEY"
    assert all("4.5" not in (entry.model or "") and "4-5" not in (entry.model or "")
               for entry in pool.agents.values())


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


def test_same_haiku_model_can_bind_to_three_exclusive_accounting_paths(monkeypatch):
    from a2a_engine.llm.factory import make_llm_client
    from a2a_engine.manifest import EpisodeManifest
    from a2a_engine.provenance import pinned_participants
    from expt_runner.run_experiment import _check_api_keys

    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-account-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat-oauth-account")
    pool = AgentPool(agents={
        "api": AgentPoolEntry(model="claude-haiku-5-5", credential="ANTHROPIC_API_KEY"),
        "oauth": AgentPoolEntry(model="claude-haiku-5-5", credential="ANTHROPIC_AUTH_TOKEN"),
        "vertex": AgentPoolEntry(model="claude-haiku-5-5", credential="vertexai_adc",
            api_format="vertexai_anthropic", config={"gcp_project": "study-project", "gcp_location": "global"}),
    })
    direct, oauth, vertex = hydrate_participants(["api", "oauth", "vertex"], pool)
    assert direct["model"] == oauth["model"] == vertex["model"]
    assert all("source" not in cfg for cfg in (direct, oauth, vertex))
    direct_client, oauth_client, vertex_client = map(make_llm_client, (direct, oauth, vertex))
    assert direct_client.api_key == "api-account-key" and direct_client.api_format == "anthropic"
    assert oauth_client.api_key == "sk-ant-oat-oauth-account" and oauth_client.api_format == "anthropic"
    assert vertex_client.api_key == "" and vertex_client.api_format == "vertexai_anthropic"
    assert vertex_client.gcp_project == "study-project"
    assert len({pinned_participants({"agents": [cfg]})[0]["config_sha256"] for cfg in (direct, oauth, vertex)}) == 3
    manifest = EpisodeManifest.from_run(config={"agents": [direct, oauth, vertex]}, experiment_name="credential-test", cell_id="one", episode_idx=0, episode_uid="test")
    assert [agent.model_dump()["credential"] for agent in manifest.agents] == ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "vertexai_adc"]
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    # An available token must not replace the selected API key.
    assert make_llm_client(direct).api_key == ""
    with pytest.raises(EnvironmentError, match="ANTHROPIC_API_KEY"):
        _check_api_keys({"agents": [direct]}, mode="live")
    _check_api_keys({"agents": [oauth, vertex]}, mode="live")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "api-account-key")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN")
    assert make_llm_client(oauth).api_key == ""
    with pytest.raises(EnvironmentError, match="ANTHROPIC_AUTH_TOKEN"):
        _check_api_keys({"agents": [oauth]}, mode="live")


@pytest.mark.parametrize("config", [
    {"credential": "vertexai_adc"},
    {"credential": "vertexai_adc", "api_format": "anthropic"},
    {"credential": "ANTHROPIC_API_KEY", "api_format": "vertexai_anthropic"},
    {"credential": "ANTHROPIC_AUTH_TOKEN", "api_format": "vertexai_anthropic"},
    {"credential": "A2A_JOIN_TICKET"},
    {"credential": "vertexai_adc", "config": {"credential": "ANTHROPIC_API_KEY", "api_format": "anthropic"}},
    {"credential": ["ANTHROPIC_API_KEY", "vertexai_adc"]},
    {"credential": "ANTHROPIC_API_KEY", "config": {"api_key": "inline-secret"}},
    {"credential": "ANTHROPIC_AUTH_TOKEN", "config": {"adc_file": "/private/adc.json"}},
])
def test_invalid_access_methods_are_rejected_at_both_typed_boundaries(config):
    from a2a_engine.environment import AgentConfig
    for model in (AgentPoolEntry, AgentConfig):
        with pytest.raises(ValueError):
            model.model_validate({"model": "claude-haiku-5-5", **config})


def test_single_credential_enum_is_the_only_access_selector_in_the_schema():
    from a2a_engine.environment import AgentConfig
    from a2a_engine.llm.credentials import ModelCredential
    for model in (AgentPoolEntry, AgentConfig):
        schema = model.model_json_schema()
        assert "source" not in schema["properties"]
        assert schema["$defs"]["ModelCredential"]["enum"] == [item.value for item in ModelCredential]


def test_legacy_inline_config_keeps_pr30_oauth_fallback(monkeypatch):
    from a2a_engine.llm.factory import make_llm_client
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-oat-legacy-account")
    legacy = make_llm_client({"model": "claude-haiku-5-5"})
    assert legacy.api_key == "sk-ant-oat-legacy-account"
    assert legacy.api_format == "anthropic"


def test_credential_selectors_round_trip_through_resolved_yaml():
    import yaml
    from a2a_engine.environment import AgentConfig
    for model, method in ((AgentPoolEntry, "as_agent_config"), (AgentConfig, "as_game_config")):
        entry = model(model="claude-haiku-5-5", credential="vertexai_adc", api_format="vertexai_anthropic")
        resolved = getattr(entry, method)()
        assert type(resolved["credential"]) is str
        assert yaml.safe_load(yaml.safe_dump(resolved)) == resolved
