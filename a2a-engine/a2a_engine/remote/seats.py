"""Private, nonserializable attempt-scoped external seat context."""

import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field


def validate_runtime(spec: dict) -> str:
    runtime = spec.get("runtime", "in_process")
    if runtime not in {"in_process", "external"}:
        raise ValueError(f"runtime: {runtime} is not supported in Phase 2")
    return runtime


@dataclass
class ExternalSeat:
    seat: int
    join_deadline: float
    ticket: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    secret: str | None = field(default=None, repr=False)
    callback_url: str | None = None
    agent_info: dict = field(default_factory=dict)
    consumed: bool = False
    registered: bool = False
    ready: threading.Event = field(default_factory=threading.Event, repr=False)
    recorder: object = field(default=None, repr=False)


class EpisodeContext:
    def __init__(self, episode_id, seats, *, join_timeout_s=10):
        self.episode_id = episode_id
        self.attempt_id = uuid.uuid4().hex
        self.seats = {seat: ExternalSeat(seat, time.time() + join_timeout_s) for seat in seats}
        self.lock = threading.RLock()
        self.active = True

    def wait_ready(self, seat):
        item = self.seats[seat]
        if not item.ready.wait(max(0, item.join_deadline - time.time())) or not self.active:
            raise TimeoutError("seat_unavailable")
        return item
