"""Infrastructure-owned execution and durability across a pinned runtime peer."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time

from cloud_platform.runtime_supervisor import atomic

RUNTIME_ENV = {
    "A2A_LAUNCH_ID", "A2A_RUNNER_LAUNCH_ID", "A2A_TRACE_DB", "A2A_RUNTIME_MODE",
    "A2A_PROVISIONING_DIR", "A2A_OTEL_TRACES_FILE", "OTEL_TRACES_EXPORTER",
    "A2A_CAPTURE_CONTENT", "PYTHONUNBUFFERED",
}


def inject(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        out.write(content)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


class LocalProcess:
    """Development fallback; hosted and Compose execution use RuntimeBridge."""
    def __init__(self, command, **kwargs):
        self.process = subprocess.Popen(command, start_new_session=True, **kwargs)
        self.error = None

    def poll(self):
        return self.process.poll()

    def wait(self, timeout=None):
        return self.process.wait(timeout=timeout)

    def cancel(self, grace=10):
        if self.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.wait()

    def close(self):
        pass


class RuntimeBridge:
    def __init__(self, execution_dir, artifacts):
        self.root = Path(execution_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.artifacts = artifacts
        inject(self.root / "runtime_supervisor.py", Path(__file__).with_name("runtime_supervisor.py").read_bytes())

    def shutdown(self):
        # Each Job owns a fresh emptyDir; persistent Compose peers use --loop
        # and ignore job-only shutdown markers between independent launches.
        atomic(self.root / "shutdown.json", {"session": os.getenv("PLATFORM_LAUNCH_ID"), "shutdown": True})

    def start(self, command, *, env, log_path, run_dir, launch_id, runtime_identity=None):
        run = Path(run_dir).resolve()
        if not run.is_relative_to(self.root):
            raise ValueError("runtime run directory must be under shared execution volume")
        request_path = self.root / "request.json"
        if request_path.exists():
            previous = json.loads(request_path.read_text())
            previous_run = Path(previous["run_dir"])
            if previous_run.exists() and not (previous_run / "result.json").exists():
                raise RuntimeError("shared runtime already has an active request")
        inject(run / "platform_runtime_sink.py", Path(__file__).with_name("runtime_sink_adapter.py").read_bytes())
        selected = {key: value for key, value in env.items() if key in RUNTIME_ENV}
        selected.update(A2A_DURABLE_EVENT_SINK="platform_runtime_sink:durable_event_write",
                        PLATFORM_EVENT_SPOOL=str(run / "event-spool"), PLATFORM_EVENT_ACK_TIMEOUT="60")
        request = {"id": launch_id, "argv": command[1:], "env": selected,
                   "cwd": os.getenv("PLATFORM_RUNTIME_WORKSPACE", "/workspace"),
                   "log_path": str(log_path), "cancel_grace": 10,
                   "worker_heartbeat_timeout": 30,
                   "runtime_identity": runtime_identity or {}}
        atomic(run / "request.json", request)
        process = BridgeProcess(self, run, launch_id)
        atomic(request_path, {"id": launch_id, "run_dir": str(run)})
        return process


class BridgeProcess:
    def __init__(self, bridge, run, identifier):
        self.bridge, self.run, self.identifier = bridge, run, identifier
        self.started = time.monotonic()
        self.start_timeout = float(os.getenv("PLATFORM_RUNTIME_START_TIMEOUT", "120"))
        self.error = None
        self.exit_code = None
        self.cancelled_at = None
        self.finished = threading.Event()
        (run / "event-spool" / "pending").mkdir(parents=True, exist_ok=True)
        (run / "event-spool" / "acks").mkdir(parents=True, exist_ok=True)
        atomic(run / "worker-heartbeat.json", {"id": identifier, "heartbeat": time.time()})
        self.pump = threading.Thread(target=self._pump, daemon=True, name=f"event-pump-{identifier}")
        self.pump.start()

    def _pump(self):
        pending = self.run / "event-spool" / "pending"
        next_heartbeat = 0
        while not self.finished.is_set():
            if time.monotonic() >= next_heartbeat:
                atomic(self.run / "worker-heartbeat.json", {"id": self.identifier, "heartbeat": time.time()})
                next_heartbeat = time.monotonic() + 1
            for path in sorted(pending.glob("*.json")):
                if self.finished.is_set():
                    return
                if time.monotonic() >= next_heartbeat:
                    atomic(self.run / "worker-heartbeat.json", {"id": self.identifier, "heartbeat": time.time()})
                    next_heartbeat = time.monotonic() + 1
                ack = self.run / "event-spool" / "acks" / path.name
                if ack.exists():
                    continue
                digest = None
                try:
                    uid, sequence, digest = path.stem.rsplit(".", 2)
                    if not uid or uid in {".", ".."} or "/" in uid or "\\" in uid or not sequence.isdecimal() or len(sequence) < 8:
                        raise ValueError("invalid event spool identity")
                    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest) or path.stat().st_size > 4 * 1024 * 1024:
                        raise ValueError("invalid event spool payload")
                    data = path.read_bytes()
                    if hashlib.sha256(data).hexdigest() != digest:
                        raise ValueError("event spool digest mismatch")
                    envelope = json.loads(data)
                    if envelope["episode_uid"] != uid:
                        raise ValueError("event spool UID mismatch")
                    self.bridge.artifacts.put(f"launches/{self.identifier}/events/{uid}/{int(sequence):08d}.json", data, timeout=15)
                    if self.finished.is_set():
                        return
                    path.unlink()
                    atomic(ack, {"sha256": digest, "error": None})
                except FileNotFoundError:
                    continue
                except Exception:
                    logging.warning("durable runtime event upload failed for launch %s", self.identifier, exc_info=True)
                    if self.finished.is_set():
                        return
                    path.unlink(missing_ok=True)
                    atomic(ack, {"sha256": digest, "error": "durable event upload failed"})
            self.finished.wait(.02)

    def _cancel_file(self):
        atomic(self.run / "cancel.json", {"id": self.identifier, "cancel": True})
        self.cancelled_at = self.cancelled_at or time.monotonic()

    def poll(self):
        if self.exit_code is not None:
            return self.exit_code
        result = self.run / "result.json"
        if result.exists():
            value = json.loads(result.read_text())
            if value["id"] != self.identifier:
                raise ValueError("runtime result identity mismatch")
            self.error = value.get("error")
            self.exit_code = int(value["exit_code"])
        else:
            heartbeat = self.run / "started.json"
            if heartbeat.exists():
                value = json.loads(heartbeat.read_text())
                if value["id"] != self.identifier:
                    raise ValueError("runtime heartbeat identity mismatch")
                expired = time.time() - float(value["heartbeat"]) > 30
                reason = "pinned runtime supervisor heartbeat expired"
            else:
                expired = time.monotonic() - self.started > self.start_timeout
                reason = "pinned runtime did not start before its startup deadline"
            if expired or self.cancelled_at and time.monotonic() - self.cancelled_at > 15:
                self._cancel_file()
                self.error, self.exit_code = reason, 125
        return self.exit_code

    def wait(self, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("pinned runtime bridge", timeout)
            time.sleep(.05)
        return self.exit_code

    def cancel(self, grace=10):
        self._cancel_file()
        try:
            self.wait(timeout=grace + 5)
        except subprocess.TimeoutExpired:
            self.error, self.exit_code = "pinned runtime did not acknowledge cancellation", 125

    def close(self):
        self.finished.set()
        self.pump.join(timeout=16)
        pointer = self.bridge.root / "request.json"
        try:
            if json.loads(pointer.read_text()).get("id") == self.identifier:
                pointer.unlink()
        except FileNotFoundError:
            pass
