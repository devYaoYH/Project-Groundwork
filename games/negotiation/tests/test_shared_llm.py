"""Legacy callers must retain their prompts and evidence through engine APIs."""

import asyncio
import copy
import io
import json
from types import SimpleNamespace

import pytest

from a2a_engine.llm import api
from negotiation_game.backend.agents import api as legacy_api
from negotiation_game.backend.agents.llm import LLMAgent, LLMAgentStdlib
from negotiation_game.backend.agents.prompts import ANTHROPIC_JSON_SUFFIX


@pytest.mark.parametrize("agent_type", [LLMAgent, LLMAgentStdlib])
def test_both_agent_classes_use_shared_streaming_and_deliver_callback(monkeypatch, agent_type):
    events = [
        {"model": "fixture", "choices": [{"delta": {"content": "hello"}}]},
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}},
    ]
    requests = []

    def urlopen(request, **kwargs):
        requests.append(json.loads(request.data))
        return io.BytesIO("".join("data: " + json.dumps(event) + "\n\n" for event in events).encode())

    monkeypatch.setattr(api.urllib.request, "urlopen", urlopen)
    agent = agent_type(api_base="https://fixture/v1", api_key="test-key", model="gpt-5-mini")
    agent._messages = [{"role": "user", "content": "input"}]
    callbacks = []

    async def started():
        callbacks.append("started")

    agent._on_stream_start = started
    result = asyncio.run(agent._call_api())
    assert result["text"] == "hello"
    assert result["total_tokens"] == 7
    assert callbacks == ["started"]
    assert requests[0]["max_completion_tokens"] == 16000
    assert "temperature" not in requests[0]


@pytest.mark.parametrize("format", ["openai", "anthropic", "vertexai", "vertexai_anthropic", "vertexai_openai"])
def test_legacy_provider_paths_use_engine_and_preserve_history(monkeypatch, format):
    captured = []

    def request(*args, **kwargs):
        captured.append((args, kwargs))
        return {"text": "hello", "completion_tokens": 3}

    transport = "_stream_openai" if format == "vertexai_openai" else "_stream_" + format
    monkeypatch.setattr(api, transport, request)
    monkeypatch.setattr(api, "_get_vertexai_access_token", lambda *args: "test-token")
    monkeypatch.setattr(api, "_vertexai_openai_base_url", lambda **kw: "https://fixture/v1")
    agent = LLMAgentStdlib(api_base="https://fixture/v1", api_key="test-key", api_format=format)
    agent._messages = [{"role": "system", "content": "instructions"}, {"role": "user", "content": "input"}]
    before = copy.deepcopy(agent._messages)
    result = asyncio.run(agent._call_api())
    assert result["text"] == "hello"
    messages = captured[0][0][3 if format in {"openai", "anthropic", "vertexai_openai"} else 1]
    assert messages[0]["content"] == "instructions" + (ANTHROPIC_JSON_SUFFIX if format == "anthropic" else "")
    assert agent._messages == before


def test_compatibility_api_only_adapts_prompts(monkeypatch):
    calls = []
    monkeypatch.setattr(api, "call_llm_streaming", lambda **kw: calls.append(kw) or {"text": "ok"})
    messages = [{"role": "system", "content": "instructions"}]
    assert legacy_api.call_llm_streaming("anthropic", "fixture", "", "fixture", messages, 20, 0)["text"] == "ok"
    assert messages[0]["content"] == "instructions"
    assert calls[0]["messages"][0]["content"] == "instructions" + ANTHROPIC_JSON_SUFFIX


@pytest.mark.parametrize("model", [
    "meta/llama-4-maverick-17b-128e-instruct-maas",
    "llama-4-maverick-17b-128e-instruct-maas",
])
def test_vertex_maas_model_ids_share_one_api_configuration(model):
    from a2a_engine.llm.model_config import MODEL_REGISTRY
    from negotiation_game.backend.agents.model_config import get_model_config
    config = get_model_config(model)
    assert config is MODEL_REGISTRY["meta/llama-4-maverick-17b-128e-instruct-maas"]
    assert config.default_max_tokens == 8192


@pytest.mark.parametrize("model", ["claude-haiku-4-5@20251001", "gemini-3-flash-preview"])
def test_resumable_judge_saves_shared_response_evidence(monkeypatch, tmp_path, model):
    from negotiation_judge import run_taxonomy_resumable_batch as batch
    calls = []
    evidence = {"text": "labels", "thinking": "reason", "duration_s": 0.25,
                "response": {"id": "provider-evidence", "usage": {"output_tokens": 7}}}
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "configured-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "region")
    monkeypatch.setattr(api, "call_llm_oneshot_full", lambda **kw: calls.append(kw) or evidence)
    monkeypatch.setattr(batch, "format_game_prompt", lambda env: "input")
    monkeypatch.setattr(batch, "parse_label_response", lambda *args: {1: {"label": True}})
    assert batch.label_game_sync(
        {"episode_uid": "episode", "rounds": [{"round_number": 1}]},
        "instructions", ["label"], [], model, 0, 1234, 100, tmp_path, False,
    ) == "success"
    saved = json.loads((tmp_path / "episode.json").read_text())
    assert saved["response_metadata"] == evidence["response"]
    assert saved["thinking"] == "reason"
    assert saved["duration_s"] == 0.25
    assert saved["max_tokens"] == calls[0]["max_tokens"] == 1234
    assert calls[0]["api_format"] == ("vertexai_anthropic" if model.startswith("claude") else "vertexai")
    if model.startswith("claude"):
        assert calls[0]["gcp_project"] == "configured-project"
        assert calls[0]["gcp_location"] == "region"
    else:
        assert calls[0]["thinking_budget"] == 100


@pytest.mark.parametrize("adc", [False, True])
def test_project_naming_routes_through_engine_with_existing_provider_choice(monkeypatch, adc):
    import google.auth
    from negotiation_game.backend import optimizer
    calls = []

    def default():
        if not adc:
            raise RuntimeError("no ADC")
        return SimpleNamespace(refresh=lambda request: None), "adc-project"

    monkeypatch.setattr(google.auth, "default", default)
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setattr(optimizer, "call_llm_oneshot", lambda **kw: calls.append(kw) or '["Iron Forge"]')
    projects = [[{"name": "old", "requirements": {"iron": 1}, "reward": 3}]]
    assert optimizer.name_projects(projects)[0][0]["name"] == "Iron Forge"
    assert projects[0][0]["name"] == "old"
    assert calls[0]["api_format"] == ("vertexai" if adc else "openai")
    assert calls[0]["timeout"] == 15
    assert calls[0]["max_tokens"] == 1024
    if adc:
        assert calls[0]["gcp_project"] == "adc-project"
        assert calls[0]["gcp_location"] == "us-central1"


def test_project_naming_provider_failure_keeps_generic_names(monkeypatch):
    import google.auth
    from negotiation_game.backend import optimizer
    monkeypatch.setattr(google.auth, "default", lambda: (SimpleNamespace(refresh=lambda req: None), "project"))

    def fail(**kwargs):
        raise ConnectionError("controlled failure")

    monkeypatch.setattr(optimizer, "call_llm_oneshot", fail)
    projects = [[{"name": "old", "requirements": {"iron": 1}, "reward": 3}]]
    assert optimizer.name_projects(projects) == projects
