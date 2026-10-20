"""Exercise the shared evidence path without contacting model providers."""

import io
import json
from types import SimpleNamespace

import pytest

from a2a_engine.llm import api


@pytest.mark.parametrize("format", ["openai", "anthropic", "vertexai_anthropic", "vertexai_openai"])
def test_full_response_preserves_provider_evidence_and_request_settings(monkeypatch, format):
    anthropic = format in {"anthropic", "vertexai_anthropic"}
    raw = ({"id": "evidence", "content": [{"type": "text", "text": " answer "},
                                             {"type": "thinking", "thinking": "reason"}],
            "usage": {"input_tokens": 7, "output_tokens": 9}}
           if anthropic else
           {"id": "evidence", "choices": [{"message": {"content": " answer "}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 9}})
    calls = []

    def urlopen(request, **kwargs):
        calls.append((request, kwargs))
        return io.BytesIO(json.dumps(raw).encode())

    monkeypatch.setattr(api.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(api, "_get_vertexai_project_location", lambda *args, **kw: ("project", "region"))
    monkeypatch.setattr(api, "_get_vertexai_access_token", lambda *args: "test-token")
    monkeypatch.setattr(api, "_vertexai_openai_base_url", lambda **kw: "https://fixture/v1")
    args = dict(api_format=format, api_base="https://fixture/v1", api_key="test-key",
                model="gpt-5-mini" if not anthropic else "claude-haiku-4-5@20251001",
                messages=[{"role": "system", "content": "instructions"},
                          {"role": "user", "content": "input"}],
                max_tokens=1234, temperature=0.2, timeout=17,
                gcp_project="project", gcp_location="region")
    full = api.call_llm_oneshot_full(**args)
    assert full["text"] == "answer"
    assert full["response"] == raw
    assert full["thinking"] == ("reason" if format == "vertexai_anthropic" else None)
    assert full["duration_s"] >= 0
    assert api.call_llm_oneshot(**args) == "answer"
    assert api.call_llm_oneshot_with_thinking(**args) == ("answer", full["thinking"])
    assert len(calls) == 3
    request, options = calls[0]
    assert options["timeout"] == 17
    body = json.loads(request.data)
    if anthropic:
        assert body["max_tokens"] == 1234
        assert body["temperature"] == 0.2
        assert body["system"] == "instructions"
    else:
        assert body["max_completion_tokens"] == 1234
        assert "temperature" not in body


def test_vertex_full_response_keeps_thinking_usage_and_bounded_sdk_timeout(monkeypatch):
    raw = {"response_id": "evidence", "usage_metadata": {"total_token_count": 16}}
    response = SimpleNamespace(
        candidates=[SimpleNamespace(content=SimpleNamespace(parts=[
            SimpleNamespace(text="answer", thought=False),
            SimpleNamespace(text="reason", thought=True),
        ]))], model_dump=lambda **kw: raw,
    )
    clients, calls = [], []

    def generate(**kwargs):
        calls.append(kwargs)
        return response

    def client(**kwargs):
        clients.append(kwargs)
        return SimpleNamespace(models=SimpleNamespace(generate_content=generate))

    monkeypatch.setattr(api, "_load_vertex_credentials", lambda adc: ("test-credentials", "adc-project"))
    monkeypatch.setattr(api, "_google_genai", SimpleNamespace(
        Client=client, types=SimpleNamespace(
            Content=lambda **kw: kw, Part=lambda **kw: kw,
            ThinkingConfig=lambda **kw: kw, GenerateContentConfig=lambda **kw: kw,
        ),
    ))
    result = api.call_llm_oneshot_full(
        "vertexai", "", "", "gemini-3-flash-preview",
        [{"role": "system", "content": "instructions"}, {"role": "user", "content": "input"}],
        max_tokens=4321, temperature=0, thinking_budget=100, timeout=17,
        gcp_project="configured-project", gcp_location="region", vertex_adc_file="adc-file",
    )
    assert result["text"] == "answer" and result["thinking"] == "reason"
    assert result["response"] == raw
    assert clients[0]["http_options"] == {"timeout": 17000}
    assert clients[0]["project"] == "configured-project"
    assert clients[0]["location"] == "region"
    assert calls[0]["config"] == {
        "max_output_tokens": 4321, "temperature": 0, "system_instruction": "instructions",
        "thinking_config": {"include_thoughts": True, "thinking_budget": 100},
    }
