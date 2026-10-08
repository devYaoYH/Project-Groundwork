"""Agent-owned structured model execution; authority remains at the MCP server."""

import asyncio
import copy
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from a2a_engine.llm.structured_output import extract_binary_logprobs, parse_actions, parse_reflection_deltas, supports_logprobs
from a2a_engine.remote.contract import Telemetry, TokenUsage

McpCall = tuple[str, str, dict]


@dataclass
class ModelRequest:
    messages: list[dict]
    options: dict
    record_history: bool = True


class ActionStrategy(Protocol):
    def build_request(self, invocation, tools) -> ModelRequest: ...
    def extract_calls(self, response) -> list[McpCall]: ...


class StructuredOutputStrategy:
    def __init__(self, model):
        self.model = model
        self.system_prompt = ""
        self.history = []
        self.mapping = {}
        self.tools = {}
        self.last_result = None
        self.thinking = None
        self.telemetry = Telemetry()
        self.model_lock = threading.Lock()

    def build_request(self, invocation, tools) -> ModelRequest:
        if invocation.prompt is None:
            raise ValueError("model invocation requires the environment prompt")
        options = {}
        reflection = invocation.kind == "reflect"
        if reflection:
            options = {"temperature": 0, "thinking_config": {"thinking_budget": 0},
                       "response_mime_type": "application/json",
                       "max_tokens": max(1024, invocation.observation["num_slots"] * 128)}
            if supports_logprobs(self.model):
                options.update(logprobs=True, top_logprobs=5)
        return ModelRequest(copy.deepcopy([{"role": "system", "content": self.system_prompt},
                                          *self.history, {"role": "user", "content": invocation.prompt}]),
                            options, not reflection)

    def extract_calls(self, response) -> list[McpCall]:
        actions, thinking = parse_actions(response.get("text") or "")
        self.thinking = thinking or response.get("reasoning") or None
        calls = []
        for action in actions:
            kind = action.get("type")
            target = self.mapping.get(kind) if isinstance(kind, str) else None
            if target not in {"comm.send", "env.schedule", "env.reschedule"}:
                continue
            endpoint, name = target.split(".")
            if name == "send":
                arguments = {"channel": kind, **{key: action[key] for key in ("content", "to") if key in action}}
            else:
                # Do not silently repair malformed fields; MCP validates them.
                arguments = {key: value for key, value in action.items() if key != "type"}
            calls.append((endpoint, name, arguments))
        return calls

    async def _call(self, invocation):
        request = self.build_request(invocation, self.tools)

        def run():
            remaining = (invocation.deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0 or not self.model_lock.acquire(timeout=max(0, remaining)):
                raise TimeoutError("model invocation expired")
            try:
                remaining = (invocation.deadline - datetime.now(timezone.utc)).total_seconds()
                if remaining <= 0:
                    raise TimeoutError("model invocation expired")
                # A cancelled coroutine cannot cancel synchronous provider I/O.
                # Clip its request and avoid a new provider retry after expiry.
                def execute(options):
                    remaining = (invocation.deadline - datetime.now(timezone.utc)).total_seconds()
                    if remaining <= 0:
                        raise TimeoutError("model invocation expired")
                    return self.model.streaming_with_retry(request.messages, timeout=remaining,
                                                           max_retries=1, **options)

                try:
                    result = execute(request.options)
                    error = str((result or {}).get("_error") or "")
                except Exception as exc:
                    error = str(exc)
                    if not (request.options.get("logprobs") and "logprob" in error.lower() and "not supported" in error.lower()):
                        raise
                if request.options.get("logprobs") and "logprob" in error.lower() and "not supported" in error.lower():
                    result = execute({key: value for key, value in request.options.items() if key not in {"logprobs", "top_logprobs"}})
                return result
            finally:
                self.model_lock.release()

        result = await asyncio.to_thread(run)
        if result is None or result.get("_error"):
            raise RuntimeError("model execution failed")
        self.last_result = result
        if request.record_history:
            self.history.extend([request.messages[-1], {"role": "assistant", "content": result.get("text") or ""}])
        usage = None
        if result.get("prompt_tokens") is not None or result.get("completion_tokens") is not None:
            prompt, completion = result.get("prompt_tokens") or 0, result.get("completion_tokens") or 0
            usage = TokenUsage(prompt_tokens=prompt, completion_tokens=completion,
                               total_tokens=result.get("total_tokens") or prompt + completion,
                               reasoning_tokens=result.get("reasoning_tokens"),
                               cached_prompt_tokens=result.get("cached_prompt_tokens"))
        self.telemetry = Telemetry(usage=usage, latency_ms=(result.get("duration_s") or 0) * 1000 or None)
        return result

    async def calls(self, invocation):
        self.telemetry = Telemetry()
        if invocation.kind == "register":
            self.system_prompt = invocation.prompt or ""
            self.mapping = copy.deepcopy(invocation.observation["action_to_tool"])
            self.tools = copy.deepcopy(invocation.observation["action_schema"])
            self.history = []
            return []
        if invocation.kind != "turn":
            return []
        return self.extract_calls(await self._call(invocation))

    async def reflect(self, invocation):
        self.telemetry = Telemetry()
        result = await self._call(invocation)
        estimates = []
        logprobs = extract_binary_logprobs(result.get("_raw_response") or result, invocation.observation["num_slots"])
        for slot, delta in enumerate(parse_reflection_deltas(result.get("text"), invocation.observation["num_slots"])):
            busy = None if delta is None else 0.5 + delta / 6
            estimates.append({"slot": slot, "belief_delta_occupied": delta,
                              "estimate_state": None if delta in {None, 0} else int(delta > 0),
                              "probability_busy": busy, "probability_free": None if busy is None else 1 - busy,
                              "confidence": None if delta is None else abs(delta) / 3, "logprobs": logprobs[slot]})
        return {"estimates": estimates}
