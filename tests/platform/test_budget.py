import pytest

from cloud_platform.budget import apply_policy


def test_token_cap_is_in_frozen_config_and_optional_model_policy(monkeypatch):
    monkeypatch.setenv("PLATFORM_ALLOWED_MODELS", "gpt-4o-mini,deepseek/deepseek-v4-flash")
    configs = [{"agents": [{"type": "llm", "model": "gpt-4o-mini"}, {"type": "scripted"}]}]
    policy = apply_policy(configs)
    assert configs[0]["agents"][0]["max_tokens"] == 4096
    assert policy["max_output_tokens_per_request"] == 4096
    with pytest.raises(ValueError, match="not enabled"):
        apply_policy([{"agents": [{"type": "llm", "model": "other"}]}])


@pytest.mark.parametrize("agent", [
    {"max_tokens": 4097}, {"max_tokens": -1}, {"max_tokens": True},
    {"extra": {"max_completion_tokens": 10000}}, {"api_key": "should-not-persist"},
    {"type": "human"}, {"runtime": "external"},
])
def test_invalid_platform_model_policy_is_rejected(agent):
    with pytest.raises(ValueError):
        apply_policy([{"agents": [agent]}])
