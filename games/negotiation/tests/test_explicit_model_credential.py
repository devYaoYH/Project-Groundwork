"""Negotiation's legacy factory must respect explicit access methods."""
import pytest

from negotiation_game.backend.agents import make_agent


def test_same_claude_id_can_use_an_anthropic_key_or_vertex(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "research-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "other-account")
    direct = make_agent({"type": "llm", "model": "claude-haiku-5-5", "credential": "ANTHROPIC_API_KEY"})
    vertex = make_agent({"type": "llm", "model": "claude-haiku-5-5", "credential": "vertexai_adc",
                         "api_format": "vertexai_anthropic", "gcp_project": "study-project",
                         "gcp_location": "global"})
    assert direct.model == vertex.model == "claude-haiku-5-5"
    assert direct.api_format == "anthropic" and direct.api_key == "research-key"
    assert vertex.api_format == "vertexai_anthropic" and vertex.api_key == ""
    assert vertex.gcp_project == "study-project"
    with pytest.raises(ValueError, match="requires api_format"):
        make_agent({"type": "llm", "model": "claude-haiku-5-5", "credential": "ANTHROPIC_API_KEY",
                    "api_format": "vertexai_anthropic"})
