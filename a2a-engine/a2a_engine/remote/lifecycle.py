"""Bounded correlated waiters, independent of action authority and call budgets."""

import hashlib
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from .capabilities import AuthorizationError
from .contract import CompletionReceipt, TurnCompletion, TurnFinished


@dataclass
class PendingTurn:
    invocation: object
    credential: str = field(repr=False)
    deadline: float
    recorder: object = None
    event: threading.Event = field(default_factory=threading.Event)
    finished: object = None
    closed_by: str | None = None
    digest: str | None = None
    retained_until: float | None = None
    acceptance: object = None


class LifecycleRegistry:
    def __init__(self, *, capacity=1024, grace_s=5, clock=time.monotonic):
        self.capacity = capacity
        self.grace_s = grace_s
        self.clock = clock
        self.records = OrderedDict()
        self.lock = threading.RLock()

    @staticmethod
    def key(identity):
        return (identity.environment_id, identity.episode_id, identity.attempt_id, identity.seat, identity.turn_id)

    def _expire(self):
        now = self.clock()
        for key, record in list(self.records.items()):
            if not record.event.is_set() and record.deadline <= now:
                self.close(record, "deadline")
            if record.retained_until is not None and record.retained_until <= now:
                del self.records[key]

    def open(self, invocation, *, deadline, recorder=None):
        with self.lock:
            self._expire()
            key = self.key(invocation)
            if key in self.records:
                raise ValueError("turn is already pending")
            if len(self.records) >= self.capacity:
                raise ValueError("pending turn capacity exceeded")
            invocation.lifecycle_credential = secrets.token_urlsafe(32)
            record = PendingTurn(invocation.model_copy(deep=True), invocation.lifecycle_credential, deadline, recorder)
            self.records[key] = record
            return record

    def finish(self, credential, finished: TurnFinished):
        with self.lock:
            self._expire()
            record = self.records.get(self.key(finished))
            if record is None or not secrets.compare_digest(record.credential, credential):
                raise AuthorizationError("invalid lifecycle credential or identity")
            if finished.reflection is not None and record.invocation.kind != "reflect":
                raise ValueError("unexpected reflection")
            if (finished.status == "succeeded" and finished.failure_code is not None
                    or finished.status == "failed" and finished.failure_code is None):
                raise ValueError("invalid terminal status")
            digest = hashlib.sha256(finished.model_dump_json().encode()).hexdigest()
            if record.digest is not None:
                if record.digest != digest:
                    raise ValueError("conflicting completion")
                disposition = "duplicate"
            elif record.event.is_set():
                disposition = "closed"
            else:
                recorder = record.recorder
                if recorder:
                    with recorder.lock:
                        if not recorder.work.empty():
                            raise ValueError("tool outcomes are still pending")
                        if recorder.closed:
                            self.close(record, recorder.closed_by)
                            return CompletionReceipt(**record.invocation.identity().model_dump(), disposition="closed")
                        if finished.status == "succeeded":
                            recorder.complete(TurnCompletion(turn_id=finished.turn_id,
                                                              telemetry=finished.telemetry, reflection=finished.reflection))
                        else:
                            recorder.close("failed")
                record.finished = finished.model_copy(deep=True)
                record.digest = digest
                self.close(record, "completed" if finished.status == "succeeded" else "failed")
                disposition = "recorded"
            return CompletionReceipt(**record.invocation.identity().model_dump(), disposition=disposition)

    def close(self, record, reason):
        with self.lock:
            if not record.event.is_set():
                if record.recorder:
                    record.recorder.close(reason)
                record.closed_by = reason
                record.retained_until = (min(self.clock(), record.deadline) if reason == "deadline" else self.clock()) + self.grace_s
                record.event.set()

    def close_episode(self, episode_id, attempt_id):
        with self.lock:
            for record in self.records.values():
                if (record.invocation.episode_id, record.invocation.attempt_id) == (episode_id, attempt_id):
                    self.close(record, "cancelled")
