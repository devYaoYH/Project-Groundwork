from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from a2a_engine.llm import api
from a2a_engine.llm.retry import RetryPolicy


def test_expired_absolute_deadline_never_contacts_provider(monkeypatch):
    def fail(**kwargs):
        raise AssertionError("expired request contacted provider")
    monkeypatch.setattr(api, "call_llm_oneshot", fail)
    client = api.LLMClient(model="mock")
    with pytest.raises(ValueError, match="deadline expired"):
        client.oneshot([], deadline=datetime.now(timezone.utc) - timedelta(seconds=1))


def test_deadline_is_rechecked_before_retry_not_renewed(monkeypatch):
    deadline = datetime.now(timezone.utc) + timedelta(seconds=10)
    calls = []
    def request(**kwargs):
        calls.append(kwargs)
        raise ConnectionError("controlled retry")
    monkeypatch.setattr(api, "call_llm_oneshot", request)
    from a2a_engine.llm import retry
    def advance(delay):
        monkeypatch.setattr(api, "datetime", SimpleNamespace(now=lambda tz: deadline + timedelta(seconds=1)))
    monkeypatch.setattr(retry.time, "sleep", advance)
    client = api.LLMClient(model="mock", retry=RetryPolicy(max_attempts=3))
    with pytest.raises(ValueError, match="deadline expired"):
        client.oneshot([], deadline=deadline)
    assert len(calls) == 1 and 0 < calls[0]["timeout"] <= 10


def test_vertex_sdk_receives_seconds_converted_to_milliseconds(monkeypatch):
    captured = {}
    def client(**kwargs):
        captured.update(kwargs)
        return "bounded-client"
    monkeypatch.setattr(api, "_google_genai", SimpleNamespace(Client=client))
    monkeypatch.setattr(api, "_load_vertex_credentials", lambda adc: ("controlled", "test-project"))
    assert api._get_vertexai_client(timeout=0.5) == "bounded-client"
    assert captured["http_options"] == {"timeout": 500}
