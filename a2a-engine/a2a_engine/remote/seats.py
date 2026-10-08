"""Private, nonserializable attempt-scoped external seat context."""

import secrets
import json
import math
import os
import subprocess
import sys
from pathlib import Path
import threading
import time
import uuid
from dataclasses import dataclass, field


def validate_runtime(spec: dict) -> str:
    runtime = spec.get("runtime", "in_process")
    if runtime == "human":
        raise ValueError("runtime: human is not yet supported")
    if not isinstance(runtime, str) or runtime not in {"in_process", "local_process", "external"}:
        raise ValueError(f"unknown runtime: {runtime}")
    harness = spec.get("harness")
    if harness is not None and (not isinstance(harness, str) or harness not in {"scripted", "structured_output"}):
        raise ValueError(f"unsupported harness: {harness}")
    return runtime


def validate_runtimes(config, *, scripted=False):
    specs = config.get("agents", [])
    for spec in specs:
        runtime = validate_runtime(spec)
        if config.get("environment_id", "calendar") == "calendar" and spec.get("type") == "human":
            raise ValueError("human is not yet supported by calendar")
        if runtime != "in_process" and config.get("environment_id", "calendar") != "calendar":
            raise ValueError("remote runtimes are supported only by calendar")
        if runtime == "local_process" and not scripted:
            if (spec.get("harness") or ("scripted" if spec.get("type") == "scripted" else "structured_output")) != "scripted":
                raise ValueError("structured_output local harness requires Phase 5")
            if spec.get("type", "llm") != "scripted":
                raise ValueError("local_process scripted harness requires type: scripted")
    timeout = config.get("join_timeout_s", 10)
    if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("join_timeout_s must be finite and positive")
    return specs


class LocalProcessLauncher:
    def __init__(self, join_url, ticket):
        # Do not inherit the runner environment: it may contain sibling tickets,
        # signing keys, tracing exporters, or unrelated provider credentials.
        env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT") if key in os.environ}
        env.update(A2A_JOIN_URL=join_url, A2A_JOIN_TICKET=ticket)
        self.process = subprocess.Popen([sys.executable, "-m", "a2a_agent.server"], env=env,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def write_descriptor(directory, episode, seat, join_url):
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError("provisioning directory must not be a symlink")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise ValueError("provisioning directory must be owner-only")
    path = directory / f"{episode.episode_id}-seat-{seat.seat}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump({"join_url": join_url, "seat": seat.seat,
                       "expires_at": seat.join_deadline, "join_ticket": seat.ticket}, handle)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


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
