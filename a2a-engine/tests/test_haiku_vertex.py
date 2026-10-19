"""Published Haiku binding reaches Vertex ADC through CLI hydration."""

from a2a_engine.agent_pool import load_agent_pool


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
    assert all(agent["credential"] == "vertexai_adc" and "api_key" not in agent for agent in config["agents"])

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
    assert pool.entry("haiku-5.5-vertex").credential == "vertexai_adc"
    assert "haiku-5.5" not in pool.agents
    assert not {"sonnet-5.5", "opus-5.5"} & pool.agents.keys()
    assert pool.entry("haiku").model == "claude-haiku-4-5-20251001"
    # An inline direct Anthropic configuration keeps its existing route.
    assert detect_provider("claude-haiku-5-5") == "anthropic"
    assert make_llm_client({"model": "claude-haiku-5-5"}).api_format == "anthropic"

