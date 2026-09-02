"""Contract tests for local-first OpenTelemetry capture defaults."""

from a2a_engine.tracing_otel import should_capture_content


def test_content_capture_defaults_to_full(monkeypatch):
    monkeypatch.delenv("A2A_CAPTURE_CONTENT", raising=False)
    assert should_capture_content() is True


def test_content_capture_can_be_explicitly_restricted(monkeypatch):
    monkeypatch.setenv("A2A_CAPTURE_CONTENT", "false")
    assert should_capture_content() is False
