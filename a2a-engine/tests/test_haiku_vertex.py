"""Published Haiku binding reaches Vertex ADC through CLI hydration."""

import base64
from pathlib import Path

import pytest

from a2a_engine.agent_pool import load_agent_pool
from a2a_engine.content import AssetStore
from a2a_engine.environment import load_release_declaration
from a2a_engine.modalities import validate_seat


@pytest.mark.parametrize("image_input", [False, True])
def test_haiku_55_participants_use_vertex_adc_without_an_anthropic_key(tmp_path, monkeypatch, image_input):
    """CLI hydration must reach Vertex with ADC and omit unsupported sampling."""
    import io
    import json
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

    content = "test"
    png_data = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="
    if image_input:
        assets = AssetStore(tmp_path / "assets")
        image = assets.put(base64.b64decode(png_data), "image/png")
        config["agents"][0]["asset_root"] = str(assets.root)
        content = [{"type": "text", "text": "Inspect the image"}, image.model_dump(exclude_none=True)]

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
        if image_input:
            block = body["messages"][0]["content"][1]
            assert block == {"type": "image", "source": {
                "type": "base64", "media_type": "image/png", "data": png_data,
            }}
        headers = {name.lower(): value for name, value in request.header_items()}
        assert headers["authorization"] == "Bearer test-adc-token"
        assert "x-api-key" not in headers
        return io.BytesIO(b'{"content": [{"type": "text", "text": "ok"}]}')

    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)
    client = make_llm_client(config["agents"][0])
    assert client.api_key == "" and client.api_base == ""
    assert client.oneshot([{"role": "user", "content": content}]) == "ok"
    pool = load_agent_pool(workspace / "experiments")
    assert pool.entry("haiku-5.5-vertex").credential == "vertexai_adc"
    assert "haiku-5.5" not in pool.agents
    assert not {"sonnet-5.5", "opus-5.5"} & pool.agents.keys()
    assert pool.entry("haiku").model == "claude-haiku-4-5-20251001"
    # An inline direct Anthropic configuration keeps its existing route.
    assert detect_provider("claude-haiku-5-5") == "anthropic"
    assert make_llm_client({"model": "claude-haiku-5-5"}).api_format == "anthropic"


@pytest.mark.parametrize("environment", ["tangram_reference", "map_director"])
def test_published_haiku_admits_all_visual_task_roles(environment):
    workspace = Path(__file__).resolve().parents[2]
    declaration = load_release_declaration(
        workspace / "games" / environment.replace("_", "-") / "environments" / f"{environment}_tiny_v1.yaml"
    )
    agent = load_agent_pool(workspace / "experiments").entry("haiku-5.5-vertex").as_agent_config()
    for role in declaration.roles:
        required = declaration.required_modalities | role.required_modalities
        assert "image" in required
        validate_seat(agent, required)
