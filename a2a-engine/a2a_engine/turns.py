"""Episode-scoped turn and LLM call recording, independent of game transports."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

from a2a_engine.tracing_otel import should_capture_content
from a2a_engine.manifest import redact_config

current_log: ContextVar[Any | None] = ContextVar("a2a_event_log", default=None)
current_turn: ContextVar[dict[str, Any] | None] = ContextVar("a2a_turn", default=None)
current_call: ContextVar[dict[str, Any] | None] = ContextVar("a2a_llm_call", default=None)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def identity(config: Any, index: int, default: str) -> tuple[str, str]:
    agents = getattr(config, "agents", ())
    if index >= len(agents):
        return default, default
    agent = agents[index]
    values = agent if isinstance(agent, dict) else agent.model_dump()
    return str(values.get("id") or default), str(values.get("role") or default)


def sdk_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, list):
        return [sdk_value(item) for item in value]
    if isinstance(value, dict):
        return {key: sdk_value(item) for key, item in value.items()}
    return value


def emit(kind: str, data: dict[str, Any], *, local_only: bool = False) -> None:
    log = current_log.get()
    if log is not None:
        if local_only:
            log.append_local(kind, data)
        else:
            log.append(kind, data)


@contextmanager
def turn(participant_id: str, role: str, observation: Any, *, agent_id: int | None = None) -> Iterator[None]:
    log = current_log.get()
    if log is None:
        yield
        return
    state = {"turn_index": log.next_turn_index(), "participant_id": str(participant_id),
             "role": role, "call_index": 0}
    if agent_id is not None:
        state["agent_id"] = agent_id
    token = current_turn.set(state)
    emit("turn.started", {**{k: state[k] for k in ("turn_index", "participant_id", "role")},
                          **({"agent_id": agent_id} if agent_id is not None else {}),
                          "observation_digest": digest(observation)})
    try:
        yield
    except BaseException:
        raise
    else:
        # The action itself is supplied via finish_turn, after the agent returns.
        pass
    finally:
        current_turn.reset(token)


def finish_turn(action: Any) -> None:
    state = current_turn.get()
    if state is not None:
        emit("turn.finished", {"turn_index": state["turn_index"],
                                "participant_id": state["participant_id"], "role": state["role"],
                                **({"agent_id": state["agent_id"]} if "agent_id" in state else {}),
                                "action_digest": digest(action)})


@contextmanager
def llm_call() -> Iterator[dict[str, Any] | None]:
    state = current_turn.get()
    if state is None:
        yield None
        return
    index = state["call_index"]
    state["call_index"] += 1
    call = {"turn_index": state["turn_index"], "call_index": index,
            "participant_id": state["participant_id"], "role": state["role"]}
    if "agent_id" in state:
        call["agent_id"] = state["agent_id"]
    token = current_call.set(call)
    try:
        yield call
    finally:
        current_call.reset(token)


@contextmanager
def ensure_call() -> Iterator[None]:
    if current_call.get() is not None:
        yield
    else:
        with llm_call():
            yield


def request(model: str, api_format: str, payload: dict[str, Any]) -> None:
    call = current_call.get()
    if call is None:
        return
    params = redact_config({k: v for k, v in payload.items()
                            if k not in ("messages", "contents", "system", "system_instruction")})
    data = {**call, "model": model, "api_format": api_format,
            "params": json.loads(json.dumps(params, default=str))}
    if should_capture_content():
        messages = {k: payload[k] for k in ("messages", "contents", "system", "system_instruction") if k in payload}
        data["messages"] = json.loads(json.dumps(messages, default=str))
    emit("llm.request", data)


def response(result: Any, *, model: str, latency_ms: float | None = None) -> None:
    call = current_call.get()
    if call is None:
        return
    result = result if isinstance(result, dict) else {"text": result}
    usage = {k: result.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                                         "reasoning_tokens", "cached_prompt_tokens") if k in result}
    data = {**call, "model": result.get("model", model), "usage": usage,
            "finish_reason": result.get("finish_reason"),
            "latency_ms": latency_ms if latency_ms is not None else
            (result.get("duration_s") * 1000 if result.get("duration_s") is not None else None)}
    if should_capture_content():
        data["text"] = result.get("text")
    emit("llm.response", data)


def chunk(delta: str) -> None:
    call = current_call.get()
    if call is not None and delta:
        emit("llm.chunk", {**call, "delta": delta if should_capture_content() else ""}, local_only=True)
