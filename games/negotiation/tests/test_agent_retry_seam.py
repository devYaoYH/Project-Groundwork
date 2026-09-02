"""Seam tests: negotiation agents on the shared retry policy.

``LLMAgentBase`` carried its own retry/backoff/cooldown copy. It now delegates to
``a2a_engine.llm.retry``. Negotiation's behavior is load-bearing for the game
engine — it reads ``last_api_meta`` to emit ``api_failure`` events and falls back
to a heuristic agent when a call returns None — so these tests pin that the
delegation changed nothing observable.
"""

import asyncio

import pytest

from a2a_engine.llm.retry import RetryPolicy
from negotiation_game.backend.agents.llm import (
    NEGOTIATION_RETRY_POLICY,
    LLMAgentBase,
)
from negotiation_game.backend.defaults import (
    API_BACKOFF_BASE,
    API_BACKOFF_MAX,
    API_MAX_RETRIES,
    API_REQUEST_COOLDOWN,
)


class Boom(Exception):
    def __init__(self, status: int, message: str = "boom"):
        super().__init__(message)
        self.status_code = status


def make_agent(policy: RetryPolicy | None = None) -> LLMAgentBase:
    """An agent whose _call_api is scripted; no network, no keys."""
    agent = LLMAgentBase(model="gpt-4o-mini")
    if policy is not None:
        agent._retry_policy = policy
    # Cooldown would make these tests sleep for real seconds.
    agent._limiter.min_interval = 0.0
    return agent


FAST = RetryPolicy(
    max_attempts=3, backoff_base=0.0, backoff_max=0.0,
    jitter_ratio=0.0, on_exhausted="return_none",
)


# --- the policy still matches negotiation's production settings ---------------


def test_policy_preserves_the_original_constants():
    assert NEGOTIATION_RETRY_POLICY.max_attempts == API_MAX_RETRIES
    assert NEGOTIATION_RETRY_POLICY.backoff_base == API_BACKOFF_BASE
    assert NEGOTIATION_RETRY_POLICY.backoff_max == API_BACKOFF_MAX
    assert NEGOTIATION_RETRY_POLICY.request_cooldown == API_REQUEST_COOLDOWN


def test_agents_degrade_rather_than_raise():
    """The engine substitutes a heuristic agent on None; an exception would
    abort the whole game instead."""
    assert NEGOTIATION_RETRY_POLICY.on_exhausted == "return_none"


def test_agents_get_a_cooldown_limiter():
    agent = LLMAgentBase(model="gpt-4o-mini")
    assert agent._limiter.min_interval == API_REQUEST_COOLDOWN


def test_each_agent_has_its_own_limiter():
    """A shared limiter would serialize agents meant to run concurrently."""
    a, b = LLMAgentBase(model="m"), LLMAgentBase(model="m")
    assert a._limiter is not b._limiter


# --- result unpacking --------------------------------------------------------


def test_successful_call_returns_text_and_records_metadata():
    agent = make_agent(FAST)

    async def call():
        return {"text": "hello", "usage": {"input_tokens": 5}, "model": "gpt-4o-mini"}

    agent._call_api = call
    assert asyncio.run(agent._call_api_with_retries()) == "hello"
    assert agent.last_api_meta == {"usage": {"input_tokens": 5}, "model": "gpt-4o-mini"}
    assert "text" not in agent.last_api_meta


def test_reasoning_summary_is_captured_when_present():
    """o-series and Claude extended thinking expose it; the engine emits it."""
    agent = make_agent(FAST)

    async def call():
        return {"text": "hi", "reasoning": "considered both projects"}

    agent._call_api = call
    asyncio.run(agent._call_api_with_retries())
    assert agent.last_reasoning == "considered both projects"


def test_a_none_result_clears_metadata_without_retrying():
    """_call_api returning None is a definitive empty answer, not an error."""
    agent = make_agent(FAST)
    agent.last_api_meta = {"stale": True}
    agent.last_reasoning = "stale"
    calls = []

    async def call():
        calls.append(1)
        return None

    agent._call_api = call
    assert asyncio.run(agent._call_api_with_retries()) is None
    assert agent.last_api_meta == {}
    assert agent.last_reasoning == ""
    assert len(calls) == 1, "a None result must not trigger the retry loop"


# --- retry behavior ----------------------------------------------------------


def test_transient_failures_are_retried_then_succeed():
    agent = make_agent(FAST)
    calls = []

    async def call():
        calls.append(1)
        if len(calls) < 3:
            raise Boom(503)
        return {"text": "recovered"}

    agent._call_api = call
    assert asyncio.run(agent._call_api_with_retries()) == "recovered"
    assert len(calls) == 3


def test_exhaustion_returns_none_and_records_failure_metadata():
    """last_api_meta feeds the engine's api_failure event."""
    agent = make_agent(FAST)

    async def call():
        raise Boom(429, "rate limited")

    agent._call_api = call
    assert asyncio.run(agent._call_api_with_retries()) is None
    assert agent.last_api_meta == {
        "retry_count": 3, "error_type": "Boom", "error_message": "rate limited",
    }


def test_non_retryable_errors_still_propagate():
    """A bad API key should surface loudly, not be silently retried away."""
    agent = make_agent(FAST)

    async def call():
        raise Boom(401, "invalid key")

    agent._call_api = call
    with pytest.raises(Boom):
        asyncio.run(agent._call_api_with_retries())


def test_deprecated_helpers_still_answer_correctly():
    """Kept as aliases so any external caller keeps working."""
    agent = make_agent(FAST)
    assert agent._is_retryable(Boom(503)) is True
    assert agent._is_retryable(Boom(400)) is False
    assert agent._get_retry_after(Boom(429)) is None
