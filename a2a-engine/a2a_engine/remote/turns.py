"""Bounded per-turn staging. Only the episode worker consumes records."""

import hashlib
import json
import threading
import time
from concurrent.futures import Future, TimeoutError
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Callable, Literal

from pydantic import BaseModel, ValidationError

from .contract import MAX_BODY_BYTES, ToolOutcome


@dataclass(frozen=True)
class ToolSpec:
    name: str
    endpoint: Literal["env", "comm"]
    input_model: type[BaseModel]
    phases: frozenset[str]
    resolves: Literal["read_only", "immediate", "end_of_turn", "end_of_phase"]
    supported: bool = True
    handler: Callable[[BaseModel, "TurnRecorder"], ToolOutcome] | None = None

    def __post_init__(self):
        if self.resolves == "immediate" and self.handler is None:
            raise ValueError("immediate tools require a worker handler")


class TurnRecorder:
    def __init__(self, invocation, claims, capabilities, handler, *, max_calls=128,
                 max_bytes=MAX_BODY_BYTES, clock=time.monotonic, deadline=None,
                 condition=None, honor_completion=True):
        self.invocation = invocation
        self.claims = claims
        self.capabilities = capabilities
        self.handler = handler
        self.max_calls = max_calls
        self.lock = threading.RLock()
        self.closed = False
        self.records = []
        self.cache = {}
        self.reserved = 0
        self.calls = 0
        self.cap_reported = False
        self.max_bytes = max_bytes
        self.bytes = 0
        self.clock = clock
        self.deadline = deadline if deadline is not None else clock() + max(
            0, (invocation.deadline - datetime.now(timezone.utc)).total_seconds())
        self.condition = condition
        self.honor_completion = honor_completion
        self.closed_by = None
        self.attempts = 0
        self.completion = None
        self.snapshot = None
        self.work = Queue()
        self.closed_event = threading.Event()

    def remaining(self):
        return max(0, self.deadline - self.clock())

    def _take_call(self):
        if self.calls >= self.max_calls:
            return False
        self.calls += 1
        return True

    def _over_cap(self):
        if not self.cap_reported:
            self.records.append({"rejection": {"code": "call_cap", "reason": "per-turn call cap exceeded"}})
            self.cap_reported = True
        return ToolOutcome(status="rejected", resolves="read_only", code="call_cap", reason="per-turn call cap exceeded")

    def call(self, spec, arguments, call_id):
        with self.lock:
            if self.remaining() <= 0:
                self.close("deadline")
            if self.closed:
                return ToolOutcome(status="rejected", resolves="read_only", code="closed_turn", reason="turn is closed")
            if not isinstance(call_id, str) or not 1 <= len(call_id) <= 128:
                if not self._take_call():
                    return self._over_cap()
                return self.reject("missing_call_id", "_meta a2a/call_id is required")
            payload = json.dumps([(spec.endpoint, spec.name) if spec else None, arguments], sort_keys=True)
            size = len(payload.encode())
            payload = hashlib.sha256(payload.encode()).hexdigest()
            if call_id in self.cache:
                previous, outcome = self.cache[call_id]
                if previous != payload:
                    if not self._take_call():
                        return self._over_cap()
                    return self.reject("call_id_conflict", "call ID was reused with different arguments")
            else:
                if not self._take_call():
                    return self._over_cap()
                if self.bytes + size > self.max_bytes:
                    outcome = self.reject("body_cap", "per-turn argument byte cap exceeded")
                    self.cache[call_id] = (payload, outcome)
                    return outcome.model_copy(deep=True)
                self.bytes += size
                if spec is None:
                    outcome = self.reject("unknown_tool", "unknown tool")
                elif not spec.supported:
                    outcome = self.reject("unsupported", "tool is not supported")
                elif self.claims.phase not in spec.phases:
                    outcome = self.reject("wrong_phase", "tool is not allowed in this phase")
                else:
                    try:
                        parsed = spec.input_model.model_validate(arguments)
                    except ValidationError:
                        outcome = self.reject("invalid_arguments", "arguments do not match tool schema")
                    else:
                        if spec.resolves == "immediate":
                            outcome = Future()
                            self.work.put((spec, parsed, outcome))
                        else:
                            outcome, action = self.handler(spec, parsed, self)
                            if action is not None:
                                self.records.append({"action": deepcopy(action)})
                            if self.condition and self.condition(self):
                                self.close("condition")
                self.cache[call_id] = (payload, outcome)
        if isinstance(outcome, Future):
            try:
                outcome = outcome.result(timeout=self.remaining())
            except TimeoutError:
                self.close("deadline")
                return ToolOutcome(status="rejected", resolves="immediate", code="closed_turn", reason="turn is closed")
        return outcome.model_copy(deep=True)

    def service_work(self):
        """Called only by the blocked environment worker, in its event context."""
        while True:
            try:
                spec, parsed, future = self.work.get_nowait()
            except Empty:
                return
            with self.lock:
                if self.remaining() <= 0:
                    self.close("deadline")
                if self.closed:
                    outcome = ToolOutcome(status="rejected", resolves="immediate", code="closed_turn", reason="turn is closed")
                else:
                    try:
                        outcome = spec.handler(parsed, self)
                    except Exception:
                        outcome = self.reject("handler_failed", "immediate handler failed")
                    if self.condition and self.condition(self):
                        self.close("condition")
                if not future.done():
                    future.set_result(outcome)

    def complete(self, completion):
        with self.lock:
            if self.remaining() <= 0:
                self.close("deadline")
            if self.closed:
                return
            if self.honor_completion:
                self.completion = completion
                self.close("completed")

    def reject(self, code, reason):
        if len(self.records) < self.max_calls:
            self.records.append({"rejection": {"code": code, "reason": reason}})
        return ToolOutcome(status="rejected", resolves="read_only", code=code, reason=reason)

    def close(self, closed_by="completed"):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.closed_by = closed_by
                self.capabilities.revoke(self.claims)
                self.snapshot = deepcopy(self.records)
                self.closed_event.set()
                while not self.work.empty():
                    _spec, _parsed, future = self.work.get_nowait()
                    if not future.done():
                        future.set_result(ToolOutcome(status="rejected", resolves="immediate", code="closed_turn", reason="turn is closed"))
            return deepcopy(self.snapshot)
