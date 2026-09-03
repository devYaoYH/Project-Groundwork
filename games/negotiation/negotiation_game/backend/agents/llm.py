"""
LLM-backed Agent Implementations

Configurable to call any OpenAI-compatible API endpoint.
Maintains a persistent conversation thread across the environment.
Agents respond with structured JSON: {"thinking", "speech", "action"}.
"""

import asyncio
import json
import logging
import random
import re
import time
import urllib.error

try:
    import httpx
except ImportError:
    httpx = None

from negotiation_game.backend.defaults import (
    API_BACKOFF_BASE,
    API_BACKOFF_MAX,
    API_MAX_RETRIES,
    API_REQUEST_COOLDOWN,
)
from a2a_engine.llm.retry import (
    FailureInfo,
    RateLimiter,
    RetryPolicy,
    acall_with_retry,
    is_retryable,
    retry_after_seconds,
)

#: Negotiation's production settings, expressed as a shared policy. Agents
#: degrade to None on exhaustion so the environment can substitute a heuristic
#: agent and emit an api_failure event rather than losing the whole run.
NEGOTIATION_RETRY_POLICY = RetryPolicy(
    max_attempts=API_MAX_RETRIES,
    backoff_base=API_BACKOFF_BASE,
    backoff_max=API_BACKOFF_MAX,
    request_cooldown=API_REQUEST_COOLDOWN,
    on_exhausted="return_none",
)
from negotiation_game.backend.agents.api import call_llm_streaming
from negotiation_game.backend.agents.heuristic import HeuristicAgent
from negotiation_game.backend.agents.model_config import get_model_config
from negotiation_game.backend.agents.prompts import (
    build_system_prompt,
    cheap_talk_prompt,
    decision_prompt,
    round_result_message,
)

log = logging.getLogger("negotiation")

_HAS_HTTPX = httpx is not None

LLM_MAX_TOKENS_FALLBACK = 4096

_JSON_RE = re.compile(r"\{[^{}]*\}")


def _extract_json(text: str) -> str | None:
    """Find the first valid JSON object in text. Returns the JSON string or None."""
    try:
        json.loads(text)
        return text
    except (json.JSONDecodeError, ValueError):
        pass
    for m in _JSON_RE.finditer(text):
        try:
            json.loads(m.group())
            return m.group()
        except (json.JSONDecodeError, ValueError):
            continue
    return None


def _repair_json(s: str) -> str | None:
    """Attempt common JSON syntax fixes. Returns repaired string or None."""
    fixed = s
    # Remove trailing commas before } or ]
    fixed = re.sub(r',\s*([}\]])', r'\1', fixed)
    # Fix single quotes → double quotes (but not inside double-quoted strings)
    # Only do this if there are no double quotes wrapping keys/values
    if '"' not in fixed and "'" in fixed:
        fixed = fixed.replace("'", '"')
    # Add missing closing braces/brackets
    opens = fixed.count('{') - fixed.count('}')
    fixed += '}' * max(0, opens)
    opens = fixed.count('[') - fixed.count(']')
    fixed += ']' * max(0, opens)
    # Replace null-like words (None, True, False from Python repr)
    fixed = re.sub(r'\bNone\b', 'null', fixed)
    fixed = re.sub(r'\bTrue\b', 'true', fixed)
    fixed = re.sub(r'\bFalse\b', 'false', fixed)
    return fixed if fixed != s else None


def _parse_structured_response(raw: str) -> tuple[str, str, dict[str, int] | None]:
    """Parse a structured JSON response into (thinking, speech, action).

    Falls back gracefully: if the response isn't valid structured JSON,
    tries to extract a JSON purchase, or treats the whole thing as speech.
    """
    stripped = raw.strip()
    # Strip markdown code fences
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    # Try parsing as structured JSON (with repair on failure)
    for attempt_str in (stripped, _repair_json(stripped)):
        if attempt_str is None:
            continue
        try:
            data = json.loads(attempt_str)
            # Accept any dict that looks like a structured response (has at least one of the expected keys)
            if isinstance(data, dict) and any(k in data for k in ("thinking", "speech", "action")):
                # Start with defaults, then merge in what the LLM provided
                thinking = str(data.get("thinking", ""))
                speech = str(data.get("speech", ""))
                action_raw = data.get("action")
                action = None
                if isinstance(action_raw, dict):
                    # Separate project runs from resource allocation
                    action_copy = dict(action_raw)
                    projects = action_copy.pop("projects", None)
                    action = {k: int(v) for k, v in action_copy.items() if isinstance(v, (int, float)) and int(v) > 0}
                    if isinstance(projects, dict):
                        project_runs = {k: int(v) for k, v in projects.items() if int(v) > 0}
                        if project_runs:
                            action["projects"] = project_runs
                if attempt_str != stripped:
                    log.info("Repaired malformed JSON response successfully")
                return thinking, speech, action
        except (json.JSONDecodeError, ValueError, TypeError):
            continue

    # Fallback: maybe it's a raw purchase JSON like {"wood": 3}
    json_str = _extract_json(stripped)
    if json_str is not None:
        try:
            parsed = json.loads(json_str)
            if isinstance(parsed, dict) and "speech" not in parsed:
                # Looks like a raw allocation, not structured response
                alloc = {k: int(v) for k, v in parsed.items() if int(v) > 0}
                return "", "", alloc
        except (json.JSONDecodeError, ValueError):
            pass

    # Final fallback: treat entire response as speech
    log.warning("Could not parse structured response, treating as plain speech: %s", stripped[:200])
    return "", stripped, None


class LLMAgentBase:
    """Base class for LLM-backed agents. Subclasses implement _call_api()."""

    def __init__(self, api_base="", api_key="", model="gpt-4o-mini",
                 temperature=0.7, api_format="openai", max_tokens=None,
                 vertex_adc_file=None, gcp_project=None, gcp_location=None):
        self.api_base = api_base.rstrip("/") if api_base else ""
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.api_format = api_format
        self.vertex_adc_file = vertex_adc_file
        self.gcp_project = gcp_project
        self.gcp_location = gcp_location
        model_cfg = get_model_config(model)
        self.max_tokens = max_tokens or model_cfg.default_max_tokens or LLM_MAX_TOKENS_FALLBACK
        self._messages = []
        self._initialized = False
        self._last_call = 0.0
        self._retry_policy = NEGOTIATION_RETRY_POLICY
        self._limiter = RateLimiter(NEGOTIATION_RETRY_POLICY.request_cooldown)
        self._fallback = None  # subclasses may set this
        self._on_stream_start = None  # optional async callback when first token arrives
        self.last_thinking = ""
        self.last_speech = ""
        self.last_reasoning = ""  # Reasoning/thinking summary from API (o-series, Claude extended thinking)
        self.last_api_meta = {}  # token usage, timing, model from last API call

    def _init_session(self, agent_id, game_config_public):
        if self._initialized:
            return
        self._agent_id = agent_id
        self._thinking_enabled = game_config_public.get("thinking", True)
        system = build_system_prompt(game_config_public, agent_id, self._thinking_enabled)
        self._messages = [{"role": "system", "content": system}]
        self._system_prompt = system
        self._initialized = True
        log.info("[%s] system prompt:\n%s", agent_id, system)

    def _update_projects(self, game_config_public):
        """Update system prompt with new projects while preserving conversation history."""
        if not self._initialized or not self._messages:
            return
        system = build_system_prompt(game_config_public, self._agent_id, self._thinking_enabled)
        self._messages[0] = {"role": "system", "content": system}
        self._system_prompt = system

    def reset_context(self):
        """Clear conversation history, keeping only the system prompt.

        Used in shifting mode so the agent starts fresh each round
        without stale context from prior (now-irrelevant) value functions.
        """
        if self._messages and self._messages[0]["role"] == "system":
            self._messages = [self._messages[0]]
        else:
            self._messages = []
        self._initialized = False

    def _append_user(self, content):
        self._messages.append({"role": "user", "content": content})

    def _append_assistant(self, content):
        self._messages.append({"role": "assistant", "content": content})

    async def _rate_limit(self):
        """Deprecated: the shared RateLimiter now enforces the cooldown."""
        await self._limiter.aacquire()

    async def _call_api(self) -> dict | None:
        """Make an API call with self._messages.
        Return {"text": str, "usage": dict, "duration_s": float, "model": str} or None."""
        raise NotImplementedError

    def _is_retryable(self, exc: Exception) -> bool:
        """Deprecated alias. Classification now lives in a2a_engine.llm.retry."""
        return is_retryable(exc)

    def _get_retry_after(self, exc: Exception) -> float | None:
        """Deprecated alias for a2a_engine.llm.retry.retry_after_seconds."""
        return retry_after_seconds(exc)

    async def _call_api_with_retries(self) -> str | None:
        """Call _call_api with the shared retry policy, returning text or None.

        The retry/backoff/cooldown mechanics moved to ``a2a_engine.llm.retry``
        so this environment and the engine can no longer drift apart. What stays here
        is the negotiation-specific part: unpacking the result dict into
        ``last_api_meta`` / ``last_reasoning``, and degrading to ``None`` on
        exhaustion so the engine can fall back to a heuristic agent and emit an
        ``api_failure`` event.
        """

        async def attempt() -> dict | None:
            result = await self._call_api()
            if result is None:
                self.last_api_meta = {}
                self.last_reasoning = ""
                return None
            self.last_api_meta = {k: v for k, v in result.items() if k != "text"}
            # o-series and Claude extended thinking expose a reasoning summary.
            self.last_reasoning = result.get("reasoning", "")
            return result

        def record_failure(failure: FailureInfo) -> None:
            self.last_api_meta = failure.as_dict()

        result = await acall_with_retry(
            attempt,
            self._retry_policy,
            limiter=self._limiter,
            on_failure=record_failure,
        )
        return result["text"] if result else None

    async def cheap_talk(self, agent_id, round_number, turn_number,
                         game_config_public, conversation_so_far, own_memory,
                         project_update=None) -> str:
        self._init_session(agent_id, game_config_public)

        prompt = cheap_talk_prompt(
            agent_id, turn_number, game_config_public,
            conversation_so_far, self._thinking_enabled,
            round_number=round_number,
            project_update=project_update,
        )
        self._append_user(prompt)

        error_reason = None
        try:
            log.info("[%s] calling LLM for cheap_talk (round %d, turn %d)",
                     agent_id, round_number, turn_number)
            reply = await self._call_api_with_retries()
            if reply is not None:
                log.info("[%s] LLM raw response: %s", agent_id, reply[:200])
                self._append_assistant(reply)

                if self._thinking_enabled:
                    thinking, speech, action = _parse_structured_response(reply)
                    self.last_thinking = thinking
                    self.last_speech = speech or ""
                    if thinking:
                        log.info("[%s] thinking: %s", agent_id, thinking[:200])
                    if action is not None:
                        return json.dumps(action)
                    return speech
                else:
                    # No-thinking mode: if agent used structured wrapper anyway,
                    # concatenate thinking + speech so nothing is lost
                    self.last_thinking = ""
                    stripped = reply.strip()
                    try:
                        data = json.loads(stripped)
                        if isinstance(data, dict) and "speech" in data:
                            log.info("[%s] unwrapping structured JSON in no-thinking mode", agent_id)
                            action = data.get("action")
                            if isinstance(action, dict):
                                alloc = {k: int(v) for k, v in action.items() if int(v) > 0}
                                return json.dumps(alloc)
                            thinking = str(data.get("thinking", "")).strip()
                            speech = str(data.get("speech", "")).strip()
                            parts = [p for p in (thinking, speech) if p]
                            return " ".join(parts) if parts else reply
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass
                    return reply
            error_reason = "API returned None"
            if self.last_api_meta and "error_type" in self.last_api_meta:
                error_reason = f"{self.last_api_meta.get('error_type', 'Unknown')}: {self.last_api_meta.get('error_message', 'No details')}"
            log.warning("[%s] LLM returned None", agent_id)
        except Exception as e:
            error_reason = f"{type(e).__name__}: {str(e)}"
            log.error("[%s] API failed during cheap_talk: %s", agent_id, e)

        # API failed or returned None — fall back
        self._messages.pop()
        if self._fallback:
            # Mark that we're using heuristic fallback
            if self.last_api_meta and "error_type" in self.last_api_meta:
                self.last_api_meta["used_fallback"] = True
            log.error("⚠️  [%s] LLM CALL FAILED - USING HEURISTIC FALLBACK | Reason: %s", agent_id, error_reason or "Unknown")
            return await self._fallback.cheap_talk(agent_id, round_number, turn_number,
                                                    game_config_public, conversation_so_far, own_memory)
        log.error("⚠️  [%s] LLM CALL FAILED - NO FALLBACK AVAILABLE | Reason: %s", agent_id, error_reason or "Unknown")
        return "[LLM error]"

    async def decide_allocation(self, agent_id, round_number,
                                game_config_public, cheap_talk_transcript,
                                own_memory) -> dict[str, int]:
        self._init_session(agent_id, game_config_public)

        prompt = decision_prompt(
            game_config_public,
            self._thinking_enabled,
            agent_id=agent_id,
            conversation_so_far=cheap_talk_transcript,
        )
        self._append_user(prompt)

        error_reason = None
        try:
            log.info("[%s] calling LLM for decide_allocation (round %d)", agent_id, round_number)
            raw = await self._call_api_with_retries()
            if raw is not None:
                log.info("[%s] LLM decision raw: %s", agent_id, raw[:200])
                self._append_assistant(raw)

                if self._thinking_enabled:
                    thinking, speech, action = _parse_structured_response(raw)
                    self.last_thinking = thinking
                    if thinking:
                        log.info("[%s] thinking: %s", agent_id, thinking[:200])
                    if action is not None:
                        return action
                else:
                    # No-thinking mode: extract JSON directly (handle structured wrapper too)
                    self.last_thinking = ""
                    visible = raw.strip()
                    if visible.startswith("```"):
                        visible = visible.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                    try:
                        data = json.loads(visible)
                        if isinstance(data, dict) and "action" in data and isinstance(data["action"], dict):
                            alloc = {k: int(v) for k, v in data["action"].items() if int(v) > 0}
                            return alloc
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass
                    json_str = _extract_json(visible)
                    if json_str is not None:
                        return {k: int(v) for k, v in json.loads(json_str).items() if int(v) > 0}

                log.warning("[%s] decision had no action, falling back", agent_id)
            error_reason = "API returned None"
            if self.last_api_meta and "error_type" in self.last_api_meta:
                error_reason = f"{self.last_api_meta.get('error_type', 'Unknown')}: {self.last_api_meta.get('error_message', 'No details')}"
            log.warning("[%s] LLM returned None for decision", agent_id)
        except Exception as e:
            error_reason = f"{type(e).__name__}: {str(e)}"
            log.error("[%s] API/parse failed during decide: %s", agent_id, e)

        # API failed or returned None — fall back
        self._messages.pop()
        if self._fallback:
            # Mark that we're using heuristic fallback
            if self.last_api_meta and "error_type" in self.last_api_meta:
                self.last_api_meta["used_fallback"] = True
            log.error("⚠️  [%s] LLM CALL FAILED - USING HEURISTIC FALLBACK | Reason: %s", agent_id, error_reason or "Unknown")
            return await self._fallback.decide_allocation(agent_id, round_number,
                                                           game_config_public, cheap_talk_transcript, own_memory)
        log.error("⚠️  [%s] LLM CALL FAILED - NO FALLBACK AVAILABLE | Reason: %s", agent_id, error_reason or "Unknown")
        return {}

    def notify_round_result(self, round_number, own_allocation, own_reward,
                            opponent_allocation, opponent_reward, overdrawn,
                            visible_opponent_reward=True, project_details=None):
        """Called by the engine after a round to feed results back into the conversation."""
        if not self._initialized:
            return
        msg = round_result_message(
            own_allocation, own_reward,
            opponent_allocation, opponent_reward,
            overdrawn, visible_opponent_reward,
            project_details=project_details,
        )
        self._append_user(msg)

    async def reflect(self, agent_id, total_rounds, own_cumulative_reward,
                      opponent_cumulative_reward, visible_opponent_reward=True,
                      theoretical_joint_max=None) -> str:
        """Request post-environment reflection from the agent.

        Called after environment completion to leverage cached tokens and extract learnings.
        Returns the agent's reflection text, or empty string on failure.
        """
        if not self._initialized:
            return ""

        from negotiation_game.backend.agents.prompts import reflection_prompt

        prompt = reflection_prompt(
            agent_id, total_rounds,
            own_cumulative_reward, opponent_cumulative_reward,
            visible_opponent_reward, theoretical_joint_max,
        )
        self._append_user(prompt)

        try:
            log.info("[%s] calling LLM for post-environment reflection", agent_id)
            reply = await self._call_api_with_retries()
            if reply is not None:
                log.info("[%s] reflection response: %s", agent_id, reply[:200])
                self._append_assistant(reply)

                # Extract content from structured response if present
                if self._thinking_enabled:
                    thinking, speech, action = _parse_structured_response(reply)
                    # For reflection, we want the full thinking + speech as the reflection
                    reflection_text = f"{thinking}\n\n{speech}".strip() if thinking else speech
                    return reflection_text or reply.strip()
                else:
                    return reply.strip()
            else:
                log.warning("[%s] LLM returned None for reflection", agent_id)
                return ""
        except Exception as e:
            log.error("[%s] reflection failed: %s", agent_id, e)
            return ""


class LLMAgent(LLMAgentBase):
    """LLM agent using httpx (async HTTP client)."""

    def __init__(self, api_base="https://api.openai.com/v1", api_key="",
                 model="gpt-4o-mini", temperature=0.7):
        if not _HAS_HTTPX:
            raise ImportError("httpx is required for LLMAgent. Install it with: pip install httpx")
        super().__init__(api_base=api_base, api_key=api_key, model=model,
                         temperature=temperature, api_format="openai")

    async def _call_api(self):
        await self._rate_limit()

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        model_cfg = get_model_config(self.model)
        payload = {
            "model": self.model,
            "messages": self._messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            model_cfg.max_tokens_param: self.max_tokens,
        }
        if model_cfg.supports_temperature:
            payload["temperature"] = self.temperature

        t0 = time.monotonic()
        chunks = []
        usage = {}
        model_name = self.model
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, read=120)) as client:
            async with client.stream(
                "POST",
                f"{self.api_base}/chat/completions",
                headers=headers,
                json=payload,
            ) as resp:
                resp.raise_for_status()
                first_chunk = True
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    chunk = json.loads(data_str)
                    if first_chunk:
                        first_chunk = False
                        if self._on_stream_start:
                            await self._on_stream_start()
                    model_name = chunk.get("model", model_name)
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices", [])
                    if choices:
                        delta = choices[0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            chunks.append(content)
        duration_s = round(time.monotonic() - t0, 3)
        text = "".join(chunks).strip()
        completion_details = usage.get("completion_tokens_details", {}) or {}
        prompt_details = usage.get("prompt_tokens_details", {}) or {}
        return {
            "text": text,
            "model": model_name,
            "duration_s": duration_s,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "reasoning_tokens": completion_details.get("reasoning_tokens"),
            "cached_prompt_tokens": prompt_details.get("cached_tokens"),
        }


class LLMAgentStdlib(LLMAgentBase):
    """LLM agent using urllib (no external dependencies). Falls back to heuristic on failure."""

    def __init__(self, api_base="", api_key="", model="gpt-4o-mini",
                 temperature=0.7, api_format="openai", max_tokens=None,
                 vertex_adc_file=None, gcp_project=None, gcp_location=None):
        super().__init__(api_base=api_base, api_key=api_key, model=model,
                         temperature=temperature, api_format=api_format, max_tokens=max_tokens,
                         vertex_adc_file=vertex_adc_file, gcp_project=gcp_project,
                         gcp_location=gcp_location)
        self._fallback = HeuristicAgent()

    async def _call_api(self):
        if self.api_format not in {"vertexai", "vertexai_anthropic", "vertexai_openai"} and not self.api_base:
            log.error("_call_api failed: api_base is empty (model=%s)", self.model)
            return None
        if self.api_format not in {"vertexai", "vertexai_anthropic", "vertexai_openai"} and not self.api_key:
            log.error("_call_api failed: api_key is empty (model=%s, api_base=%s)", self.model, self.api_base)
            return None

        await self._rate_limit()

        loop = asyncio.get_event_loop()
        try:
            if self.api_format == "anthropic":
                return await loop.run_in_executor(None, self._request_anthropic, loop)
            if self.api_format == "vertexai":
                return await loop.run_in_executor(None, self._request_vertexai, loop)
            if self.api_format == "vertexai_anthropic":
                return await loop.run_in_executor(None, self._request_vertexai_anthropic, loop)
            if self.api_format == "vertexai_openai":
                return await loop.run_in_executor(None, self._request_vertexai_openai, loop)
            else:
                return await loop.run_in_executor(None, self._request_openai, loop)
        except Exception as e:
            log.error("_call_api executor failed: %s: %s (model=%s, api_base=%s)",
                     type(e).__name__, str(e), self.model, self.api_base)
            raise

    # _request_openai and _request_anthropic return dicts, not strings

    def _request_openai(self, loop=None):
        return call_llm_streaming(
            api_format="openai",
            api_base=self.api_base,
            api_key=self.api_key,
            model=self.model,
            messages=self._messages,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            on_first_chunk=self._on_stream_start,
            loop=loop,
        )

    def _request_anthropic(self, loop=None):
        return call_llm_streaming(
            api_format="anthropic",
            api_base=self.api_base,
            api_key=self.api_key,
            model=self.model,
            messages=self._messages,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            on_first_chunk=self._on_stream_start,
            loop=loop,
        )

    def _request_vertexai(self, loop=None):
        return call_llm_streaming(
            api_format="vertexai",
            api_base="",
            api_key="",
            model=self.model,
            messages=self._messages,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            on_first_chunk=self._on_stream_start,
            loop=loop,
            vertex_adc_file=self.vertex_adc_file,
            gcp_project=self.gcp_project,
            gcp_location=self.gcp_location,
        )

    def _request_vertexai_anthropic(self, loop=None):
        return call_llm_streaming(
            api_format="vertexai_anthropic",
            api_base="",
            api_key="",
            model=self.model,
            messages=self._messages,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            on_first_chunk=self._on_stream_start,
            loop=loop,
            vertex_adc_file=self.vertex_adc_file,
            gcp_project=self.gcp_project,
            gcp_location=self.gcp_location,
        )

    def _request_vertexai_openai(self, loop=None):
        return call_llm_streaming(
            api_format="vertexai_openai",
            api_base="",
            api_key="",
            model=self.model,
            messages=self._messages,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            on_first_chunk=self._on_stream_start,
            loop=loop,
            vertex_adc_file=self.vertex_adc_file,
            gcp_project=self.gcp_project,
            gcp_location=self.gcp_location,
        )
