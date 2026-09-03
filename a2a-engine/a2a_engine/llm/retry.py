"""Shared robustness policy for LLM API calls: classification, backoff, cooldown.

Before the merge this logic existed twice, in near-identical copies:
``a2a_engine.llm.api`` (sync, used by calendar via ``streaming_with_retry``) and
``negotiation_game.backend.agents.llm.LLMAgentBase`` (async, used by every
negotiation agent). They agreed on the hard parts — which errors are transient,
honoring ``Retry-After``, jittered exponential backoff with a cap — and diverged
on three things that mattered:

1. **sync vs async sleeping.** The negotiation engine is async, so a
   ``time.sleep`` in its retry loop would stall the whole event loop, including
   the other agent's in-flight call. Both variants live here now
   (:func:`call_with_retry` / :func:`acall_with_retry`) over one policy.
2. **request cooldown.** Only negotiation had a per-agent minimum interval
   between calls (:class:`RateLimiter`), which is what keeps a long
   self-play run from tripping rate limits in the first place.
3. **what exhaustion means.** Negotiation returns ``None`` so the caller can
   fall back to a heuristic agent and emit an ``api_failure`` event; the engine
   raised. Both are legitimate, so it is now an explicit policy choice
   (:attr:`RetryPolicy.on_exhausted`) rather than an accident of which copy you
   called.

The delay computation is a pure function (:func:`compute_delay`) so the backoff
schedule can be tested without sleeping or mocking a clock.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import urllib.error
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar

log = logging.getLogger("a2a_engine.llm.retry")

T = TypeVar("T")

#: Statuses worth retrying: rate limiting plus the transient 5xx family.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Substrings identifying retryable failures from providers that raise plain
#: exceptions with no status attribute (several SDK wrappers do this).
_RETRYABLE_SUBSTRINGS = ("timed out", "timeout", "too many requests")


@dataclass(frozen=True)
class RetryPolicy:
    """How hard, how long, and how to give up.

    Defaults match negotiation's production settings, which are the
    battle-tested ones: 10 attempts with a 120s cap survives sustained
    provider-side rate limiting during large self-play studies.
    """

    max_attempts: int = 10
    backoff_base: float = 2.0
    backoff_max: float = 120.0
    #: Jitter added to exponential delays, as a fraction of the delay.
    jitter_ratio: float = 0.25
    #: Jitter added to server-supplied Retry-After, as a fraction, absolutely
    #: capped by ``retry_after_jitter_max`` so a huge Retry-After does not turn
    #: into an unbounded extra wait.
    retry_after_jitter_ratio: float = 0.2
    retry_after_jitter_max: float = 10.0
    #: Minimum seconds between successive calls through a shared RateLimiter.
    request_cooldown: float = 0.0
    #: "return_none" degrades gracefully; "raise" propagates the last error.
    on_exhausted: str = "raise"

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.on_exhausted not in ("raise", "return_none"):
            raise ValueError("on_exhausted must be 'raise' or 'return_none'")


#: Used by LLMClient when no policy is supplied. Conservative on attempts
#: because a foreground environment turn should not stall for minutes.
DEFAULT_POLICY = RetryPolicy(max_attempts=5, backoff_max=60.0)


@dataclass
class FailureInfo:
    """Structured record of an exhausted retry chain.

    Negotiation surfaces this as an ``api_failure`` environment event; keeping it typed
    means every environment reports API failures the same way instead of each inventing
    its own dict.
    """

    retry_count: int
    error_type: str
    error_message: str
    attempts: int = 0

    @classmethod
    def from_exception(cls, exc: BaseException, attempts: int) -> FailureInfo:
        return cls(
            retry_count=attempts,
            error_type=type(exc).__name__,
            error_message=str(exc),
            attempts=attempts,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "retry_count": self.retry_count,
            "error_type": self.error_type,
            "error_message": self.error_message,
        }


class RetryExhausted(RuntimeError):
    """Raised when retries run out under ``on_exhausted="raise"``."""

    def __init__(self, failure: FailureInfo, last_exception: BaseException) -> None:
        super().__init__(
            f"LLM call failed after {failure.attempts} attempt(s): "
            f"{failure.error_type}: {failure.error_message}"
        )
        self.failure = failure
        self.last_exception = last_exception


# --- classification ----------------------------------------------------------


def status_code_of(exc: BaseException) -> int | None:
    """Best-effort HTTP status extraction across provider SDKs and urllib."""
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def is_retryable(exc: BaseException) -> bool:
    """Whether an exception represents a transient failure worth retrying.

    Status codes win over string matching: a 400 whose body happens to contain
    "timeout" is a malformed request, and retrying it just burns quota. Only
    when there is no status do we fall back to inspecting the message, which is
    what providers wrapping errors in bare exceptions leave us.
    """
    status = status_code_of(exc)
    if status is not None:
        return status in RETRYABLE_STATUSES

    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    # OSError covers socket-level failures, but not its non-transient
    # subclasses, which signal a broken release rather than a blip.
    if isinstance(exc, OSError) and not isinstance(
        exc, (FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError)
    ):
        return True

    msg = str(exc).lower()
    if any(token in msg for token in _RETRYABLE_SUBSTRINGS):
        return True
    return any(str(code) in msg for code in RETRYABLE_STATUSES)


def retry_after_seconds(exc: BaseException) -> float | None:
    """Read a ``Retry-After`` header. Anthropic and OpenAI both send it on 429."""
    headers = getattr(exc, "headers", None)
    if headers is None and isinstance(exc, urllib.error.HTTPError):
        headers = exc.headers
    if headers is None:
        return None
    try:
        value = headers.get("retry-after") or headers.get("Retry-After")
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        # Retry-After may be an HTTP-date; we do not parse those, and guessing
        # would be worse than falling back to exponential backoff.
        return None
    return seconds if seconds >= 0 else None


# --- delay computation (pure) ------------------------------------------------


def compute_delay(
    attempt: int,
    exc: BaseException,
    policy: RetryPolicy,
    rng: random.Random | None = None,
) -> float:
    """Seconds to wait before ``attempt + 1``. Pure: no sleeping, no clock.

    A server-supplied Retry-After wins over local backoff, because it reflects
    the provider's actual reset window. Both paths are jittered — without it,
    parallel agents that hit the same 429 would retry in lockstep and trip the
    limit again.
    """
    rng = rng or random
    hinted = retry_after_seconds(exc)
    if hinted is not None:
        base = min(hinted, policy.backoff_max)
        jitter_span = min(base * policy.retry_after_jitter_ratio, policy.retry_after_jitter_max)
    else:
        base = min(policy.backoff_base * (2 ** attempt), policy.backoff_max)
        jitter_span = base * policy.jitter_ratio
    return base + rng.uniform(0, jitter_span)


# --- rate limiting -----------------------------------------------------------


class RateLimiter:
    """Enforces a minimum interval between calls.

    Ported from negotiation's per-agent ``_rate_limit``. Retrying is recovery;
    this is prevention, and it is why long self-play runs stayed under provider
    limits. One limiter per agent preserves that behavior — a process-wide
    limiter would serialize agents that are meant to run concurrently.
    """

    def __init__(self, min_interval: float = 0.0) -> None:
        self.min_interval = min_interval
        self._last_call = 0.0

    def _delay(self) -> float:
        if self.min_interval <= 0:
            return 0.0
        elapsed = time.monotonic() - self._last_call
        return max(0.0, self.min_interval - elapsed)

    def _stamp(self) -> None:
        self._last_call = time.monotonic()

    def acquire(self) -> None:
        delay = self._delay()
        if delay > 0:
            time.sleep(delay)
        self._stamp()

    async def aacquire(self) -> None:
        delay = self._delay()
        if delay > 0:
            await asyncio.sleep(delay)
        self._stamp()


# --- retry loops -------------------------------------------------------------


def _should_stop(exc: BaseException, attempt: int, policy: RetryPolicy) -> bool:
    return not is_retryable(exc) or attempt >= policy.max_attempts - 1


def _give_up(
    exc: BaseException,
    attempts: int,
    policy: RetryPolicy,
    on_failure: Callable[[FailureInfo], None] | None,
) -> None:
    """Record the failure, then either raise or signal a graceful ``None``."""
    failure = FailureInfo.from_exception(exc, attempts)
    if on_failure is not None:
        on_failure(failure)
    if not is_retryable(exc):
        # A non-retryable error is a bug or a misconfiguration; surfacing it
        # unchanged is more useful than a generic RetryExhausted wrapper.
        raise exc
    log.error("LLM retries exhausted after %d attempts: %s", attempts, exc)
    if policy.on_exhausted == "raise":
        raise RetryExhausted(failure, exc) from exc


def call_with_retry(
    fn: Callable[[], T],
    policy: RetryPolicy = DEFAULT_POLICY,
    *,
    limiter: RateLimiter | None = None,
    on_failure: Callable[[FailureInfo], None] | None = None,
    rng: random.Random | None = None,
) -> T | None:
    """Synchronous retry loop. Returns ``None`` only under ``return_none``."""
    for attempt in range(policy.max_attempts):
        if limiter is not None:
            limiter.acquire()
        try:
            return fn()
        except Exception as exc:
            if _should_stop(exc, attempt, policy):
                _give_up(exc, attempt + 1, policy, on_failure)
                return None
            delay = compute_delay(attempt, exc, policy, rng)
            log.warning(
                "Retryable LLM error (attempt %d/%d), backing off %.1fs: %s",
                attempt + 1, policy.max_attempts, delay, exc,
            )
            time.sleep(delay)
    return None  # unreachable: the final attempt always returns or gives up


async def acall_with_retry(
    fn: Callable[[], Awaitable[T]],
    policy: RetryPolicy = DEFAULT_POLICY,
    *,
    limiter: RateLimiter | None = None,
    on_failure: Callable[[FailureInfo], None] | None = None,
    rng: random.Random | None = None,
) -> T | None:
    """Async twin of :func:`call_with_retry`.

    Sleeps with ``asyncio.sleep`` so a backing-off agent yields the event loop
    instead of stalling every other agent in the environment.
    """
    for attempt in range(policy.max_attempts):
        if limiter is not None:
            await limiter.aacquire()
        try:
            return await fn()
        except Exception as exc:
            if _should_stop(exc, attempt, policy):
                _give_up(exc, attempt + 1, policy, on_failure)
                return None
            delay = compute_delay(attempt, exc, policy, rng)
            log.warning(
                "Retryable LLM error (attempt %d/%d), backing off %.1fs: %s",
                attempt + 1, policy.max_attempts, delay, exc,
            )
            await asyncio.sleep(delay)
    return None  # unreachable
