"""Seam tests: the consolidated LLM retry/backoff/cooldown policy.

This logic previously existed twice — sync in ``a2a_engine.llm.api``, async in
negotiation's ``LLMAgentBase`` — with subtly different behavior. These tests pin
the merged semantics, especially the places the two copies disagreed:

- what counts as retryable (status code beats string matching)
- whether exhaustion raises or returns None (now an explicit policy choice)
- that async backoff yields the event loop instead of blocking it

Delay computation is a pure function, so the backoff schedule is tested directly
rather than by timing sleeps.
"""

import asyncio
import random
import urllib.error

import pytest

from a2a_engine.llm.retry import (
    DEFAULT_POLICY,
    FailureInfo,
    RateLimiter,
    RetryExhausted,
    RetryPolicy,
    acall_with_retry,
    call_with_retry,
    compute_delay,
    is_retryable,
    retry_after_seconds,
    status_code_of,
)

FAST = RetryPolicy(max_attempts=3, backoff_base=0.0, backoff_max=0.0, jitter_ratio=0.0)


class HTTPish(Exception):
    """Stand-in for a provider SDK error carrying a status code."""

    def __init__(self, status: int, message: str = "", headers: dict | None = None):
        super().__init__(message or f"HTTP {status}")
        self.status_code = status
        self.headers = headers


# --- classification ----------------------------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_statuses_are_retryable(status):
    assert is_retryable(HTTPish(status))


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retryable(status):
    """Retrying a malformed request or a bad key just burns quota."""
    assert not is_retryable(HTTPish(status))


def test_status_code_beats_message_matching():
    """A 400 whose body mentions a timeout is still a bad request.

    The two pre-merge copies both checked strings; doing it before the status
    check would have made this case retry forever.
    """
    assert not is_retryable(HTTPish(400, "upstream timeout while validating"))


def test_timeouts_and_connection_errors_are_retryable():
    assert is_retryable(TimeoutError("timed out"))
    assert is_retryable(ConnectionError("connection reset"))
    assert is_retryable(ConnectionResetError("reset by peer"))


def test_non_transient_oserrors_are_not_retryable():
    """A missing ADC file is a broken release, not a blip."""
    assert not is_retryable(FileNotFoundError("adc.json missing"))
    assert not is_retryable(PermissionError("denied"))


def test_message_matching_is_the_fallback_for_statusless_errors():
    assert is_retryable(Exception("429 Too Many Requests"))
    assert is_retryable(Exception("received 503 from upstream"))
    assert not is_retryable(Exception("invalid model name"))


def test_status_code_is_read_from_several_attribute_names():
    exc = Exception("boom")
    exc.code = 503
    assert status_code_of(exc) == 503


# --- Retry-After -------------------------------------------------------------


def test_retry_after_is_read_case_insensitively():
    assert retry_after_seconds(HTTPish(429, headers={"retry-after": "7"})) == 7.0
    assert retry_after_seconds(HTTPish(429, headers={"Retry-After": "3"})) == 3.0


def test_missing_or_unparseable_retry_after_is_none():
    assert retry_after_seconds(HTTPish(429)) is None
    assert retry_after_seconds(HTTPish(429, headers={})) is None
    # HTTP-date form: we don't parse it, and guessing beats nothing badly.
    assert retry_after_seconds(
        HTTPish(429, headers={"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})
    ) is None


def test_retry_after_is_read_from_urllib_http_error(tmp_path):
    exc = urllib.error.HTTPError(
        url="http://x", code=429, msg="rate limited",
        hdrs={"retry-after": "12"}, fp=None,
    )
    assert retry_after_seconds(exc) == 12.0


# --- delay schedule (pure) ---------------------------------------------------


def test_backoff_is_exponential_and_capped():
    policy = RetryPolicy(backoff_base=2.0, backoff_max=30.0, jitter_ratio=0.0)
    delays = [compute_delay(i, Exception("500"), policy) for i in range(6)]
    assert delays[:4] == [2.0, 4.0, 8.0, 16.0]
    assert all(d == 30.0 for d in delays[4:]), "must cap, not grow to 512s"


def test_server_retry_after_overrides_local_backoff():
    policy = RetryPolicy(backoff_base=2.0, retry_after_jitter_ratio=0.0)
    exc = HTTPish(429, headers={"retry-after": "42"})
    # Attempt 0 would locally back off 2s; the server said 42.
    assert compute_delay(0, exc, policy) == 42.0


def test_retry_after_is_also_capped():
    policy = RetryPolicy(backoff_max=30.0, retry_after_jitter_ratio=0.0)
    exc = HTTPish(429, headers={"retry-after": "3600"})
    assert compute_delay(0, exc, policy) == 30.0


def test_jitter_stays_within_its_band_and_is_never_negative():
    policy = RetryPolicy(backoff_base=10.0, backoff_max=100.0, jitter_ratio=0.25)
    rng = random.Random(0)
    for _ in range(200):
        d = compute_delay(0, Exception("500"), policy, rng)
        assert 10.0 <= d <= 12.5


def test_jitter_actually_varies():
    """Without jitter, parallel agents hitting one 429 retry in lockstep."""
    policy = RetryPolicy(backoff_base=10.0, jitter_ratio=0.25)
    rng = random.Random(1)
    assert len({compute_delay(0, Exception("500"), policy, rng) for _ in range(50)}) > 1


def test_retry_after_jitter_is_absolutely_capped():
    """A huge Retry-After must not imply an unbounded extra wait."""
    policy = RetryPolicy(
        backoff_max=1000.0, retry_after_jitter_ratio=0.2, retry_after_jitter_max=10.0
    )
    exc = HTTPish(429, headers={"retry-after": "600"})
    rng = random.Random(2)
    for _ in range(100):
        assert 600.0 <= compute_delay(0, exc, policy, rng) <= 610.0


# --- sync retry loop ---------------------------------------------------------


def test_succeeds_without_retrying():
    calls = []
    result = call_with_retry(lambda: calls.append(1) or "ok", FAST)
    assert result == "ok" and len(calls) == 1


def test_retries_then_succeeds():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise HTTPish(503)
        return "ok"

    assert call_with_retry(flaky, FAST) == "ok"
    assert len(calls) == 3


def test_non_retryable_error_propagates_immediately():
    calls = []

    def bad():
        calls.append(1)
        raise HTTPish(401, "bad key")

    with pytest.raises(HTTPish):
        call_with_retry(bad, FAST)
    assert len(calls) == 1, "must not retry an auth failure"


def test_exhaustion_raises_under_the_raise_policy():
    def always():
        raise HTTPish(503)

    with pytest.raises(RetryExhausted) as info:
        call_with_retry(always, FAST)
    assert info.value.failure.attempts == 3
    assert info.value.failure.error_type == "HTTPish"


def test_exhaustion_returns_none_under_the_return_none_policy():
    """Negotiation depends on this to fall back to a heuristic agent."""
    policy = RetryPolicy(
        max_attempts=2, backoff_base=0.0, backoff_max=0.0, on_exhausted="return_none"
    )

    def always():
        raise HTTPish(503)

    assert call_with_retry(always, policy) is None


def test_failure_callback_receives_structured_metadata():
    seen = []
    policy = RetryPolicy(
        max_attempts=2, backoff_base=0.0, backoff_max=0.0, on_exhausted="return_none"
    )
    call_with_retry(
        lambda: (_ for _ in ()).throw(HTTPish(429, "slow down")),
        policy,
        on_failure=seen.append,
    )
    assert len(seen) == 1
    assert isinstance(seen[0], FailureInfo)
    assert seen[0].as_dict() == {
        "retry_count": 2, "error_type": "HTTPish", "error_message": "slow down",
    }


def test_single_attempt_policy_does_not_retry():
    calls = []
    policy = RetryPolicy(max_attempts=1, on_exhausted="return_none")

    def always():
        calls.append(1)
        raise HTTPish(503)

    assert call_with_retry(always, policy) is None
    assert len(calls) == 1


def test_invalid_policies_are_rejected_at_construction():
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(on_exhausted="explode")


# --- async retry loop --------------------------------------------------------


def test_async_retries_then_succeeds():
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise HTTPish(503)
        return "ok"

    assert asyncio.run(acall_with_retry(flaky, FAST)) == "ok"
    assert len(calls) == 3


def test_async_exhaustion_returns_none_under_policy():
    policy = RetryPolicy(
        max_attempts=2, backoff_base=0.0, backoff_max=0.0, on_exhausted="return_none"
    )

    async def always():
        raise HTTPish(503)

    assert asyncio.run(acall_with_retry(always, policy)) is None


def test_async_backoff_yields_the_event_loop():
    """The reason the async variant exists.

    Negotiation runs both agents on one loop. A time.sleep in the retry path
    would stall the other agent's in-flight call for the whole backoff.
    """
    policy = RetryPolicy(max_attempts=2, backoff_base=0.05, backoff_max=0.05,
                         jitter_ratio=0.0, on_exhausted="return_none")
    progress = []

    async def always_fails():
        raise HTTPish(503)

    async def bystander():
        for _ in range(5):
            await asyncio.sleep(0.005)
            progress.append(1)

    async def main():
        await asyncio.gather(acall_with_retry(always_fails, policy), bystander())

    asyncio.run(main())
    assert len(progress) == 5, "the other coroutine must keep running during backoff"


def test_async_non_retryable_propagates():
    async def bad():
        raise HTTPish(400)

    with pytest.raises(HTTPish):
        asyncio.run(acall_with_retry(bad, FAST))


# --- rate limiter ------------------------------------------------------------


def test_rate_limiter_is_a_noop_when_disabled():
    limiter = RateLimiter(0.0)
    limiter.acquire()
    assert limiter._delay() == 0.0


def test_rate_limiter_enforces_the_interval():
    limiter = RateLimiter(10.0)
    limiter.acquire()  # first call is free, then stamps
    assert limiter._delay() > 9.0


def test_async_rate_limiter_stamps_without_sleeping_when_idle():
    limiter = RateLimiter(0.0)
    asyncio.run(limiter.aacquire())
    assert limiter._delay() == 0.0


def test_limiter_is_consulted_before_each_attempt():
    class Counting(RateLimiter):
        def __init__(self):
            super().__init__(0.0)
            self.acquires = 0

        def acquire(self):
            self.acquires += 1

    limiter = Counting()
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise HTTPish(503)
        return "ok"

    call_with_retry(flaky, FAST, limiter=limiter)
    assert limiter.acquires == 3, "cooldown must apply to retries, not just the first call"


# --- defaults ----------------------------------------------------------------


def test_default_policy_is_conservative_but_real():
    assert DEFAULT_POLICY.max_attempts > 1
    assert DEFAULT_POLICY.backoff_max <= 60.0
    assert DEFAULT_POLICY.on_exhausted == "raise"
