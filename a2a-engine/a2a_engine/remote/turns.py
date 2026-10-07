"""Bounded per-turn staging. Only the episode worker consumes records."""

import json
import threading
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ValidationError

from .contract import ToolOutcome


@dataclass(frozen=True)
class ToolSpec:
    name: str
    endpoint: Literal["env", "comm"]
    input_model: type[BaseModel]
    phases: frozenset[str]
    resolves: Literal["read_only", "immediate", "end_of_turn", "end_of_phase"]
    supported: bool = True


class TurnRecorder:
    def __init__(self, invocation, claims, capabilities, handler, *, max_calls=128):
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

    def _take_call(self):
        if self.calls >= self.max_calls:
            return False
        self.calls += 1
        return True

    @staticmethod
    def _over_cap():
        return ToolOutcome(status="rejected", resolves="read_only", code="call_cap", reason="per-turn call cap exceeded")

    def call(self, spec, arguments, call_id):
        with self.lock:
            if self.closed:
                return ToolOutcome(status="rejected", resolves="read_only", code="closed_turn", reason="turn is closed")
            if not isinstance(call_id, str) or not 1 <= len(call_id) <= 128:
                if not self._take_call():
                    return self._over_cap()
                return self.reject("missing_call_id", "_meta a2a/call_id is required")
            payload = json.dumps([(spec.endpoint, spec.name) if spec else None, arguments], sort_keys=True)
            if call_id in self.cache:
                previous, outcome = self.cache[call_id]
                if previous == payload:
                    return outcome
                if not self._take_call():
                    return self._over_cap()
                return self.reject("call_id_conflict", "call ID was reused with different arguments")
            if not self._take_call():
                return self._over_cap()
            if spec is None:
                outcome = self.reject("unknown_tool", "unknown tool")
            elif not spec.supported:
                outcome = self.reject("unsupported", "tool requires Phase 3")
            elif self.claims.phase not in spec.phases:
                outcome = self.reject("wrong_phase", "tool is not allowed in this phase")
            else:
                try:
                    parsed = spec.input_model.model_validate(arguments)
                except ValidationError:
                    outcome = self.reject("invalid_arguments", "arguments do not match tool schema")
                else:
                    outcome, action = self.handler(spec, parsed, self)
                    if action is not None:
                        self.records.append({"action": action})
            self.cache[call_id] = (payload, outcome)
            return outcome

    def reject(self, code, reason):
        if len(self.records) < self.max_calls:
            self.records.append({"rejection": {"code": code, "reason": reason}})
        return ToolOutcome(status="rejected", resolves="read_only", code=code, reason=reason)

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.capabilities.revoke(self.claims)
            return list(self.records)
