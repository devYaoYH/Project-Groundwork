"""Core judge function: one game → one judgment via LLM call.

One LLM call per game (sees all rounds). Output is a GameJudgment with
per-round breakdowns, so downstream analysis stays round-granular while
the judge gains cross-round awareness (repair, learning, regression).
"""

import json
import logging
import os
import re
import threading
import time
import urllib.error

from json_repair import repair_json

from negotiation_game.backend.defaults import API_MAX_RETRIES, API_BACKOFF_BASE, API_BACKOFF_MAX, API_REQUEST_COOLDOWN
from negotiation_game.backend.agents.api import call_llm_streaming
from negotiation_judge.schema import (
    GameJudgment,
    JudgeGameContext,
    JudgeRoundContext,
    RoundJudgment,
)
from negotiation_judge.prompts import build_judge_messages

log = logging.getLogger("judge.core")


class TokenBudgetExceeded(Exception):
    """Raised when the LLM response was truncated by the token limit.

    This is not retryable with the same budget — the caller must increase
    max_tokens (JUDGE_MAX_TOKENS) before trying again.
    """

# Matches ```json ... ``` blocks or ``` ... ```
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)

# Judge calls can be large (whole game transcripts). Override via JUDGE_MAX_TOKENS env var.
JUDGE_MAX_TOKENS = int(os.environ.get("JUDGE_MAX_TOKENS", 16000))

# Rate-limit state — protected by a lock so concurrent threads don't race.
_cooldown_lock = threading.Lock()
_last_call_time: float = 0.0


def _strip_markdown_fences(text: str) -> str:
    """Remove markdown code fences wrapping JSON."""
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


def _wait_for_cooldown():
    """Enforce minimum delay between API calls (thread-safe)."""
    global _last_call_time
    with _cooldown_lock:
        elapsed = time.monotonic() - _last_call_time
        if elapsed < API_REQUEST_COOLDOWN:
            time.sleep(API_REQUEST_COOLDOWN - elapsed)
        _last_call_time = time.monotonic()


def _load_json_with_repair(text: str) -> dict:
    """Parse JSON, attempting local repair before raising.

    Tries strict json.loads first. On failure, runs json_repair which fixes
    common LLM output issues (unescaped characters, trailing commas, truncated
    output, missing delimiters) without burning an LLM retry.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError as original_err:
        try:
            repaired = repair_json(text, return_objects=True)
            if isinstance(repaired, dict):
                log.warning("JSON repaired locally (original error: %s)", original_err)
                return repaired
        except Exception:
            pass
        raise original_err


def _parse_game_judgment_json(raw: str, ctx: JudgeGameContext) -> GameJudgment:
    """Parse and validate raw LLM output into a GameJudgment.

    Overrides game-level metadata + per-round metadata from the context so
    the LLM can't accidentally drift on identifiers or ground-truth fields.
    """
    cleaned = _strip_markdown_fences(raw)
    data = _load_json_with_repair(cleaned)

    # Force game-level metadata from context
    data["game_id"] = ctx.game_id
    data["model_a"] = ctx.model_a
    data["model_b"] = ctx.model_b
    data["mode"] = ctx.mode
    data["mc_ratio"] = ctx.mc_ratio

    # Build a lookup of round ground truth
    round_truth = {r.round_number: r for r in ctx.rounds}

    rounds_out = data.get("rounds") or []
    for rj in rounds_out:
        rn = rj.get("round_number")
        truth = round_truth.get(rn)
        if truth is None:
            # LLM hallucinated a round; keep its values but force game-level metadata
            rj["game_id"] = ctx.game_id
            rj["model_a"] = ctx.model_a
            rj["model_b"] = ctx.model_b
            rj["mode"] = ctx.mode
            rj["mc_ratio"] = ctx.mc_ratio
            continue
        # Force all ground-truth fields
        rj["game_id"] = ctx.game_id
        rj["round_number"] = truth.round_number
        rj["model_a"] = ctx.model_a
        rj["model_b"] = ctx.model_b
        rj["mode"] = ctx.mode
        rj["mc_ratio"] = ctx.mc_ratio
        rj["round_outcome"] = truth.round_outcome.value
        rj["joint_efficiency"] = truth.joint_efficiency

    data["rounds"] = rounds_out
    return GameJudgment.model_validate(data)


def judge_game(
    ctx: JudgeGameContext,
    api_format: str,
    api_base: str,
    api_key: str,
    model: str,
    temperature: float = 0.0,
    max_retries: int = 3,
    thinking_config: dict | None = None,
) -> GameJudgment:
    """Run the LLM judge on a whole game and return a validated GameJudgment.

    One LLM call sees all rounds; output is per-round breakdowns plus a
    game-level narrative.

    Retries on JSON parse / validation errors (up to max_retries).
    HTTP 429/5xx retries handled inside _call_with_backoff.

    Raises:
        ValueError: If all retries exhausted for parse/validation errors.
        urllib.error.HTTPError: If all retries exhausted for HTTP errors.
    """
    messages = build_judge_messages(ctx)
    last_error = None

    for attempt in range(max_retries):
        _wait_for_cooldown()

        # TokenBudgetExceeded propagates immediately out of the retry loop
        raw_text = _call_with_backoff(
            api_format=api_format,
            api_base=api_base,
            api_key=api_key,
            model=model,
            messages=messages,
            temperature=temperature,
            thinking_config=thinking_config,
        )

        try:
            judgment = _parse_game_judgment_json(raw_text, ctx)
            # Sanity: warn if round count mismatches
            expected = {r.round_number for r in ctx.rounds}
            got = {r.round_number for r in judgment.rounds}
            missing = expected - got
            extra = got - expected
            if missing:
                log.warning("%s: judge missed rounds %s", ctx.game_id, sorted(missing))
            if extra:
                log.warning("%s: judge hallucinated rounds %s", ctx.game_id, sorted(extra))
            return judgment
        except (json.JSONDecodeError, Exception) as e:
            last_error = e
            log.warning(
                "Parse/validation error on %s (attempt %d/%d): %s",
                ctx.game_id, attempt + 1, max_retries, e,
            )
            if attempt < max_retries - 1:
                messages.append({"role": "assistant", "content": raw_text})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Your previous response was not valid JSON. Error: {e}\n"
                        "Please respond with ONLY a valid JSON object matching the schema. "
                        "No markdown fences, no preamble. Include every round."
                    ),
                })

    raise ValueError(
        f"Failed to get valid judgment for {ctx.game_id} "
        f"after {max_retries} attempts. Last error: {last_error}"
    )


# --- Legacy per-round parser (kept for test compatibility) ---

def _parse_judgment_json(raw: str, ctx: JudgeRoundContext) -> RoundJudgment:
    """Parse and validate raw LLM output into a RoundJudgment (legacy per-round flow)."""
    cleaned = _strip_markdown_fences(raw)
    data = json.loads(cleaned)
    data["game_id"] = ctx.game_id
    data["round_number"] = ctx.round_number
    data["model_a"] = ctx.model_a
    data["model_b"] = ctx.model_b
    data["mode"] = ctx.mode
    data["mc_ratio"] = ctx.mc_ratio
    data["round_outcome"] = ctx.round_outcome.value
    data["joint_efficiency"] = ctx.joint_efficiency
    return RoundJudgment.model_validate(data)


def _call_with_backoff(
    api_format: str,
    api_base: str,
    api_key: str,
    model: str,
    messages: list[dict],
    temperature: float,
    thinking_config: dict | None = None,
) -> str:
    """Call LLM with exponential backoff for transient errors.

    Uses streaming so we get per-chunk progress and avoid silent 35-minute hangs
    on non-streaming oneshot calls.
    """
    for attempt in range(API_MAX_RETRIES):
        try:
            result = call_llm_streaming(
                api_format=api_format,
                api_base=api_base,
                api_key=api_key,
                model=model,
                messages=messages,
                max_tokens=JUDGE_MAX_TOKENS,
                temperature=temperature,
                thinking_config=thinking_config,
            )
            finish_reason = result.get("finish_reason")
            log.info(
                "Judge call complete: %d chars, %s tokens in %.1fs [stop=%s]",
                len(result.get("text", "")),
                result.get("total_tokens", "?"),
                result.get("duration_s", 0),
                finish_reason,
            )
            if finish_reason in ("length", "max_tokens"):
                raise TokenBudgetExceeded(
                    f"Judge response truncated at token limit (finish_reason={finish_reason!r}, "
                    f"tokens={result.get('total_tokens')}, chars={len(result.get('text',''))}). "
                    f"Increase JUDGE_MAX_TOKENS (currently {JUDGE_MAX_TOKENS}) and re-run."
                )
            if result.get("reasoning"):
                log.info("  [thinking summary] %s", result["reasoning"][:500])
            return result["text"]
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529) and attempt < API_MAX_RETRIES - 1:
                wait = min(API_BACKOFF_BASE * (2 ** attempt), API_BACKOFF_MAX)
                log.warning("HTTP %d, retrying in %.1fs (attempt %d/%d)", e.code, wait, attempt + 1, API_MAX_RETRIES)
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Unreachable")
