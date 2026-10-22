"""Haiku replacement reaches OpenRouter after CLI participant resolution."""

import base64
import io
import json
from pathlib import Path

import pytest

from a2a_engine.content import AssetStore
from a2a_engine.experiment import load_experiment
from a2a_engine.llm import api
from a2a_engine.llm.factory import make_llm_client
from a2a_engine.modalities import validate_seat
from expt_runner.run_experiment import _check_api_keys


@pytest.mark.parametrize("name", ["haiku", "haiku-or"])
@pytest.mark.parametrize("image_input", [False, True])
def test_haiku_replacement_uses_openrouter_key_and_supported_payload(tmp_path, monkeypatch, name, image_input):
    workspace = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("A2A_AGENT_POOL", str(workspace / "experiments/agents.yaml"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused-anthropic-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "unused-oauth-token")
    experiment = tmp_path / "haiku.yaml"
    experiment.write_text(
        f"name: haiku_openrouter\nparticipants: [{name}, {name}]\n"
        "defaults: {environment_id: word_guess, num_agents: 2}\n"
        "cells: [{label: baseline, count: 1}]\n"
    )
    config = load_experiment(experiment).defaults
    _check_api_keys(config, mode="live")
    assert all(agent["model"] == "anthropic/claude-haiku-5.5" for agent in config["agents"])
    agent = config["agents"][0]
    validate_seat(agent, {"text", "image"} if image_input else {"text"})
    content = "test"
    png_data = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="
    if image_input:
        assets = AssetStore(tmp_path / "assets")
        image = assets.put(base64.b64decode(png_data), "image/png")
        agent["asset_root"] = str(assets.root)
        content = [{"type": "text", "text": "Inspect the image"}, image.model_dump(exclude_none=True)]

    def fake_urlopen(request, **kwargs):
        assert request.full_url == "https://openrouter.ai/api/v1/chat/completions"
        assert request.get_header("Authorization") == "Bearer test-openrouter-key"
        body = json.loads(request.data)
        assert body["model"] == "anthropic/claude-haiku-5.5"
        assert body["max_tokens"] == 8192
        assert "temperature" not in body and "max_completion_tokens" not in body
        if image_input:
            assert body["messages"][0]["content"][1] == {
                "type": "image_url", "image_url": {"url": f"data:image/png;base64,{png_data}"},
            }
        return io.BytesIO(b'{"choices": [{"message": {"content": "ok"}}]}')

    monkeypatch.setattr(api.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(api, "_load_vertex_credentials", lambda *args: pytest.fail("OpenRouter must not load ADC"))
    client = make_llm_client(agent)
    assert client.oneshot([{"role": "user", "content": content}]) == "ok"
    monkeypatch.delenv("OPENROUTER_API_KEY")
    # Other available credentials cannot silently replace the selected account.
    assert make_llm_client(agent).api_key == ""
    with pytest.raises(EnvironmentError, match="OPENROUTER_API_KEY"):
        _check_api_keys(config, mode="live")
