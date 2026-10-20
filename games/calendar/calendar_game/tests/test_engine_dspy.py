"""DSPy proposer/reflection calls must use the same provider path as agents."""

import asyncio

import dspy
import pytest
from dspy.clients.cache import Cache

from a2a_engine.llm import api
from a2a_engine.llm.dspy import EngineLM


@pytest.fixture
def engine_calls(monkeypatch):
    calls = []

    def request(**kwargs):
        calls.append(kwargs)
        return {"text": "[[ ## answer ## ]]\npolicy\n[[ ## completed ## ]]",
                "thinking": None, "duration_s": 0.1,
                "response": {"usage_metadata": {"total_token_count": 9}}}

    monkeypatch.setattr(api, "call_llm_oneshot_full", request)
    monkeypatch.setattr(dspy, "cache", Cache(enable_disk_cache=False, enable_memory_cache=True, disk_cache_dir=None))
    return calls


def test_dspy_predict_and_reflection_share_engine_transport(engine_calls):
    lm = EngineLM("vertex_ai/gemini-3-flash-preview", max_tokens=1234, temperature=0.3,
                  vertex_project="configured-project", vertex_location="region")
    with dspy.context(lm=lm):
        assert dspy.Predict("question -> answer")(question="input").answer == "policy"
    assert lm("reflection input")[0].startswith("[[ ## answer ## ]]")
    assert len(engine_calls) == 2
    assert engine_calls[0]["model"] == "gemini-3-flash-preview"
    assert engine_calls[0]["api_format"] == "vertexai"
    assert engine_calls[0]["max_tokens"] == 1234
    assert engine_calls[0]["gcp_project"] == "configured-project"
    assert engine_calls[0]["gcp_location"] == "region"
    assert lm.history[-1]["usage"]["total_tokens"] == 9


def test_dspy_cache_rollouts_copy_and_async_stay_on_engine(engine_calls):
    lm = EngineLM("vertex_ai/gemini-3-flash-preview", temperature=0.7)
    first = lm("input")
    assert lm("input") == first
    assert len(engine_calls) == 1
    assert lm.history[-1]["usage"] == {}
    assert lm("input", rollout_id=1) == first
    assert len(engine_calls) == 2
    assert "rollout_id" not in engine_calls[-1]
    copied = lm.copy(temperature=0.9, max_tokens=42)
    assert asyncio.run(copied.acall("async input")) == first
    assert engine_calls[-1]["temperature"] == 0.9
    assert engine_calls[-1]["max_tokens"] == 42
    assert copied.history and len(lm.history) == 3


def test_dspy_retries_are_owned_by_engine(engine_calls, monkeypatch):
    calls = []

    def request(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise ConnectionError("transient fixture")
        return {"text": "policy", "response": {}}

    monkeypatch.setattr(api, "call_llm_oneshot_full", request)
    from a2a_engine.llm import retry
    monkeypatch.setattr(retry.time, "sleep", lambda seconds: None)
    lm = EngineLM("vertex_ai/gemini-3-flash-preview", num_retries=1, cache=False)
    assert lm("input") == ["policy"]
    assert len(calls) == 2


@pytest.mark.parametrize("explicit_credential", [False, True])
@pytest.mark.parametrize("model, format, variable", [
    ("openai/gpt-4o-mini", "openai", "OPENAI_API_KEY"),
    ("anthropic/claude-sonnet-4-6", "anthropic", "ANTHROPIC_API_KEY"),
    ("gemini/gemini-3-flash-preview", "openai", "GOOGLE_API_KEY"),
])
def test_explicit_dspy_provider_keeps_its_credentials(engine_calls, monkeypatch, model, format, variable, explicit_credential):
    monkeypatch.setenv(variable, "fixture-key")
    lm = EngineLM(model, cache=False, **({"credential": variable} if explicit_credential else {}))
    lm("input")
    assert engine_calls[0]["api_format"] == format
    assert engine_calls[0]["api_key"] == "fixture-key"


def test_unsupported_dspy_options_fail_before_contacting_provider(engine_calls):
    lm = EngineLM("vertex_ai/gemini-3-flash-preview")
    with pytest.raises(ValueError, match="generation options"):
        lm("input", tools=[{}])
    assert engine_calls == []


def test_dspy_vertex_credential_selector_stays_with_shared_factory(engine_calls):
    lm = EngineLM("vertex_ai/gemini-3-flash-preview", credential="vertexai_adc", cache=False,
                  vertex_project="configured-project", vertex_location="region")
    lm("input")
    assert engine_calls[0]["api_format"] == "vertexai"
    assert engine_calls[0]["api_key"] == ""
    assert engine_calls[0]["gcp_project"] == "configured-project"
