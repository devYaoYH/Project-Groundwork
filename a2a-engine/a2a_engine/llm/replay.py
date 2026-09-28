"""Fail-closed, episode-scoped replay of durable completed turns."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Callable

from a2a_engine import turns
from a2a_engine.schemas import Event


class ReplayMismatch(ValueError):
    """Recorded evidence cannot reproduce the requested turn."""


current_replay: ContextVar["ReplayingClient | None"] = ContextVar("a2a_replay", default=None)


class ReplayingClient:
    def __init__(self, entries: list[dict], *, live: Callable[..., Any] | None = None) -> None:
        self.live = live
        events = [Event.model_validate(entry["event"]) for entry in entries]
        started: set[int] = set()
        finished: set[int] = set()
        for event in events:
            if event.type == "turn.started":
                started.add(int(event.data["turn_index"]))
            elif event.type == "turn.finished":
                finished.add(int(event.data["turn_index"]))
        unfinished = sorted(started - finished)
        if not unfinished:
            raise ReplayMismatch("resume requires a durably started, unfinished turn")
        self.restart_turn = unfinished[-1]
        boundary = next(i for i, event in enumerate(events)
                        if event.type == "turn.started" and
                        int(event.data["turn_index"]) == self.restart_turn)
        self.prefix = [event for event in events[:boundary] if event.type != "llm.chunk"]
        self.position = 0
        self.responses: dict[tuple[int, int], dict] = {}
        self.requests: dict[tuple[int, int], dict] = {}
        for event in self.prefix:
            if event.type == "llm.request":
                self.requests[(int(event.data["turn_index"]), int(event.data["call_index"]))] = event.data
            if event.type == "llm.response":
                key = (int(event.data["turn_index"]), int(event.data["call_index"]))
                if key in self.responses:
                    raise ReplayMismatch(f"duplicate recorded response for {key}")
                self.responses[key] = event.data
        for key, response in self.responses.items():
            if "result" not in response or response["result"] is None:
                raise ReplayMismatch(f"missing replayable content for turn/call {key}")

    def event(self, event: Event) -> Event:
        if self.position >= len(self.prefix):
            return event
        expected = self.prefix[self.position]
        if expected.type != event.type or (expected.type != "game_start" and expected.data != event.data):
            raise ReplayMismatch(f"replay diverged at event {self.position}: expected {expected.type}, got {event.type}")
        self.position += 1
        return expected

    def call(self, kind: str, model: str, messages: list[dict], live: Callable[[], Any]) -> Any:
        state = turns.current_turn.get()
        if state is None or state["turn_index"] >= self.restart_turn:
            return live()
        with turns.llm_call() as call:
            if call is None:
                raise ReplayMismatch("replay call has no turn")
            key = (call["turn_index"], call["call_index"])
            response = self.responses.get(key)
            if response is None or "result" not in response:
                raise ReplayMismatch(f"missing replayable response for turn/call {key}")
            if self.requests.get(key, {}).get("model") != model:
                raise ReplayMismatch(f"model changed for turn/call {key}")
            request = self.requests[key]
            if request.get("api_format") == "openai" and "messages" in request:
                if request["messages"].get("messages") != messages:
                    raise ReplayMismatch(f"prompt changed for turn/call {key}")
            log = turns.current_log.get()
            if log is None:
                raise ReplayMismatch("replay has no event log")
            while self.position < len(self.prefix):
                event = self.prefix[self.position]
                if event.type not in {"llm.request", "llm.attempt", "llm.response"}:
                    break
                if (event.data.get("turn_index"), event.data.get("call_index")) != key:
                    break
                log.append(event.type, event.data)
            if self.position == 0 or not any(
                e.type == "llm.response" and
                (e.data.get("turn_index"), e.data.get("call_index")) == key
                for e in self.prefix[:self.position]
            ):
                raise ReplayMismatch(f"response event missing at turn/call {key}")
            result = response["result"]
            if kind == "oneshot":
                if not isinstance(result, str):
                    raise ReplayMismatch(f"expected text for turn/call {key}")
            elif not isinstance(result, dict):
                raise ReplayMismatch(f"expected streaming result for turn/call {key}")
            return result
