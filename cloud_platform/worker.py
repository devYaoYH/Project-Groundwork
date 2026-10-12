"""Execute one admitted launch with its environment and agents colocated.

The immutable launch snapshot is input, not a live shared SQLite database.
Firestore receives small projections; the full evidence stays in artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from cloud_platform.storage import ACTIVE, ArtifactStore, Repository
from cloud_platform.runtime_bridge import LocalProcess, RuntimeBridge


def now():
    return datetime.now(UTC).isoformat()


class _LiveDetailPublisher:
    """Bounded observational snapshots, separate from final scientific evidence."""

    def __init__(self, worker, control, launch_id, log_path):
        self.worker, self.control = worker, control
        self.launch_id, self.log_path = launch_id, log_path
        self.key = f"launches/{launch_id}/live-detail.json"
        self.offset = 0
        self.pending = b""

    def consume_logs(self, *, final=False):
        try:
            self._consume_logs(final=final)
        except Exception:
            logging.warning("live runner log read unavailable for launch %s; will retry", self.launch_id, exc_info=True)

    def _consume_logs(self, *, final=False):
        with self.log_path.open("rb") as source:
            source.seek(self.offset)
            while True:
                chunk = source.read(256 * 1024)
                self.offset += len(chunk)
                lines = (self.pending + chunk).split(b"\n")
                self.pending = lines.pop()
                for line in lines:
                    self.control._line(self.launch_id, line.decode("utf-8", errors="replace").rstrip("\r"))
                # A malformed endless stdout line must not grow worker memory.
                # The complete bytes still remain in immutable runner.log.
                if len(self.pending) > 64 * 1024:
                    self.control._line(self.launch_id, self.pending[:64 * 1024].decode("utf-8", errors="replace") + " [long line truncated]")
                    self.pending = b""
                if not final or not chunk:
                    break
        if final and self.pending:
            self.control._line(self.launch_id, self.pending.decode("utf-8", errors="replace"))
            self.pending = b""

    def publish(self):
        """Publish only joined provisional attempts; failures never stop inference."""
        try:
            record = self.worker.repository.get("launches", self.launch_id)
            if not record or record["status"] not in {"RUNNING", "CANCELLING"}:
                return
            with self.control._session() as db:
                rows = db.execute("""
                    SELECT a.*, e.episode_uid, e.status AS trace_status, e.ended_at AS trace_ended_at
                    FROM attempts a LEFT JOIN episodes e ON e.episode_uid = (
                        SELECT episode_uid FROM episodes
                        WHERE episode_id = a.episode_id AND attempt = a.attempt
                        ORDER BY created_at DESC, episode_uid DESC LIMIT 1)
                    WHERE a.launch_id = ? ORDER BY a.cell_id, a.episode_idx
                """, (self.launch_id,)).fetchall()
                logs = db.execute("SELECT * FROM launch_events WHERE launch_id=? AND kind='runner.log' ORDER BY id DESC LIMIT 201",
                                  (self.launch_id,)).fetchall()
            attempts = []
            for row in rows:
                attempt = {**asdict(self.control._attempt(row)), "episode_uid": row["episode_uid"],
                           "redis_stream": f"a2a:launch:{self.launch_id}:episode:{row['episode_id']}"}
                if row["trace_status"] == "COMPLETED":
                    attempt.update(status="COMPLETED", ended_at=row["trace_ended_at"])
                attempts.append(attempt)
            tail, size = [], 2
            logs_truncated = len(logs) > 200
            for row in logs[:200]:
                entry = dict(row)
                entry["payload"] = json.loads(entry["payload"])
                line = str(entry["payload"].get("line", ""))
                if len(line) > 4096:
                    logs_truncated = True
                entry["payload"]["line"] = line[:4096]
                count = len(json.dumps(entry).encode()) + 2
                if size + count > 64 * 1024:
                    logs_truncated = True
                    break
                tail.append(entry)
                size += count
            tail.reverse()
            from cloud_platform.viewer import Viewer
            updated = now()
            detail = {"launch": record, "attempts": attempts, "progress": Viewer.progress(attempts),
                      "runner_logs": tail, "provisional": True, "live_updated_at": updated,
                      "logs_truncated": logs_truncated,
                      "log_start_id": tail[0]["id"] if tail else 0}
            self.worker.artifacts.put(self.key, json.dumps(detail, sort_keys=True).encode(), immutable=False)
            self.worker.repository.claim("launches", self.launch_id, {"RUNNING", "CANCELLING"},
                                         {"live_detail_key": self.key, "live_updated_at": updated})
        except Exception:
            logging.warning("live detail publication unavailable for launch %s; will retry", self.launch_id, exc_info=True)


class Worker:
    def __init__(self, repository=None, artifacts=None, *, workspace=None):
        self.repository = repository or Repository()
        self.artifacts = artifacts or ArtifactStore()
        self.workspace = Path(workspace or os.getenv("PLATFORM_WORKSPACE", "/workspace")).resolve()
        self.stopping = threading.Event()
        execution_dir = os.getenv("PLATFORM_EXECUTION_DIR")
        if self.artifacts.backend == "gcp" and not execution_dir:
            raise ValueError("Hosted workers require a separate pinned runtime and PLATFORM_EXECUTION_DIR")
        self.bridge = RuntimeBridge(execution_dir, self.artifacts) if execution_dir else None

    def stop(self, *_):
        self.stopping.set()

    def run_once(self, launch_id: str | None = None) -> bool:
        candidates = [self.repository.get("launches", launch_id)] if launch_id else self.repository.list("launches")
        for launch in candidates:
            if not launch or launch.get("status") not in {"QUEUED", "DISPATCHING", "DISPATCHED"}:
                continue
            if launch.get("cancel_requested"):
                return self.repository.claim("launches", launch["id"], {"QUEUED", "DISPATCHING", "DISPATCHED"}, {"status": "CANCELLED", "ended_at": now(), "error": "cancelled before execution"}) is not None
            claimed = self.repository.claim("launches", launch["id"], {"QUEUED", "DISPATCHING", "DISPATCHED"}, {"status": "RUNNING", "started_at": now(), "heartbeat_at": now()})
            if not claimed:
                continue
            try:
                with self.heartbeat(claimed["id"]):
                    self._execute(claimed)
            except Exception as exc:
                # A retry must never re-execute an already claimed attempt.
                # Transient publication failures leave durable events intact.
                self.repository.claim("launches", claimed["id"], ACTIVE, {"status": "FAILED", "ended_at": now(), "error": str(exc), "evidence_complete": False})
                raise
            return True
        return False

    @contextmanager
    def heartbeat(self, launch_id):
        """Keep the claim alive during slow runner and artifact operations."""
        finished = threading.Event()
        def beat():
            while not finished.wait(15):
                try:
                    result = self.repository.claim("launches", launch_id, {"RUNNING", "CANCELLING"}, {"heartbeat_at": now()})
                    if result is None:
                        break
                except Exception:
                    import logging
                    logging.warning("worker heartbeat unavailable for launch %s", launch_id)
        thread = threading.Thread(target=beat, name=f"heartbeat-{launch_id}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            finished.set()
            thread.join(timeout=1)

    def _execute(self, launch):
        from cloud_platform.frozen import open_frozen_control

        if launch.get("worker_contract_version", 1) != 1:
            raise ValueError("Unsupported worker execution contract")
        for key, variable, fallback in (("worker_image_digest", "PLATFORM_WORKER_IMAGE_DIGEST", "PLATFORM_IMAGE_DIGEST"),
                                        ("worker_source_sha", "PLATFORM_WORKER_SOURCE_SHA", "PLATFORM_SOURCE_SHA")):
            expected = launch.get(key)
            actual = os.getenv(variable) or os.getenv(fallback)
            if expected and expected not in {"local", "test-source"} and actual != expected:
                raise ValueError(f"Worker provenance mismatch for {key}")
        identifier = launch["id"]
        prefix = f"launches/{identifier}"
        with tempfile.TemporaryDirectory(prefix="groundwork-launch-", dir=self.bridge.root if self.bridge else None) as directory:
            root = Path(directory)
            database = root / "trace.sqlite"
            database.write_bytes(self.artifacts.get(launch["snapshot_key"]))
            plan = root / "execution.yaml"
            plan.write_bytes(self.artifacts.get(launch["plan_key"]))
            results = root / "results"
            results.mkdir()
            # Construction normally reconciles all launches as abandoned.
            # This snapshot has been explicitly admitted, so materialize the
            # control plane without that local-server restart side effect.
            control = open_frozen_control(database, workspace=self.workspace, results_dir=results)
            with control._session() as db:
                db.execute("UPDATE launches SET trace_database=?, execution_path=? WHERE id=?", (str(database), str(plan), identifier))
            command = [sys.executable, "-m", "expt_runner.run_experiment", str(plan),
                       "--storage-path", str(database), "--results-dir", str(results),
                       "--max-parallelism", str(min(2, max(1, int(launch.get("max_parallelism", 1)))))]
            if launch.get("mode") == "smoke":
                command += ["--smoke-test", "--smoke-episodes-per-cell", "1"]
            elif launch.get("mode") == "dry_run":
                command.append("--dry-run")
            env = dict(os.environ)
            env.update(A2A_LAUNCH_ID=identifier, A2A_RUNNER_LAUNCH_ID=identifier,
                       A2A_TRACE_DB=str(database), A2A_RUNTIME_MODE="loopback",
                       A2A_PROVISIONING_DIR=str(root / "private-joins"),
                       A2A_OTEL_TRACES_FILE=str(results / "otel-spans.jsonl"),
                       OTEL_TRACES_EXPORTER="file", A2A_CAPTURE_CONTENT="true", PYTHONUNBUFFERED="1")
            env.update(A2A_DURABLE_EVENT_SINK="cloud_platform.storage:durable_event_write",
                       A2A_DURABLE_EVENT_PREFIX=prefix + "/events",
                       PLATFORM_BACKEND=self.artifacts.backend)
            if self.artifacts.backend == "local":
                env["PLATFORM_DATA_DIR"] = str(self.artifacts.root.parent)
            log_path = root / "runner.log"
            control._mark_launch_started(identifier)
            with log_path.open("w") as log:
                live = _LiveDetailPublisher(self, control, identifier, log_path)
                live.publish()
                if self.bridge:
                    process = self.bridge.start(command, env=env, log_path=log_path, run_dir=root, launch_id=identifier,
                        runtime_identity={"source_sha": launch.get("runtime_source_sha", launch.get("source_sha")),
                                          "image_digest": launch.get("runtime_image_digest", launch.get("image_digest"))})
                else:
                    process = LocalProcess(command, cwd=self.workspace, env=env, stdout=log, stderr=subprocess.STDOUT)
                cancelled = False
                timed_out = False
                deadline = time.monotonic() + 3600
                next_publication = 0.0
                try:
                    while process.poll() is None:
                        if time.monotonic() >= next_publication:
                            live.consume_logs()
                            live.publish()
                            next_publication = time.monotonic() + 2
                        record = self.repository.get("launches", identifier) or {}
                        timed_out = time.monotonic() >= deadline
                        if timed_out or self.stopping.is_set() or record.get("status") in {"CANCELLING", "INTERRUPTED"} or record.get("cancel_requested"):
                            cancelled = record.get("status") == "CANCELLING" or bool(record.get("cancel_requested"))
                            process.cancel()
                            break
                        self.stopping.wait(1)
                    code = process.wait()
                finally:
                    try:
                        if process.poll() is None:
                            process.cancel()
                    finally:
                        process.close()
            live.consume_logs(final=True)
            if cancelled:
                with control._session() as db:
                    db.execute("UPDATE launches SET status='CANCELLING' WHERE id=?", (identifier,))
            control._finish_launch(identifier, code)
            detail = control.launch_detail(identifier)
            live.publish()
            if timed_out:
                detail["launch"].update(status="FAILED", error="launch exceeded its 3600-second duration limit")
            if process.error:
                detail["launch"].update(status="FAILED", error=process.error)
            if self.stopping.is_set() and not cancelled:
                detail["launch"].update(status="INTERRUPTED", error="worker stopped before completion")
            # SQLite backup includes committed WAL content in a standalone
            # artifact; copying the .sqlite file directly would lose evidence.
            snapshot = root / "completed.sqlite"
            with sqlite3.connect(database) as source, sqlite3.connect(snapshot) as destination:
                source.backup(destination)
            checksums = {}
            def upload(key, payload):
                self.artifacts.put(key, payload)
                checksums[key] = hashlib.sha256(payload).hexdigest()
                return key
            upload(prefix + "/trace.sqlite", snapshot.read_bytes())
            upload(prefix + "/runner.log", log_path.read_bytes())
            for path in sorted(results.rglob("*")):
                if path.is_file() and not any(part.startswith(".") or part == "private-joins" for part in path.relative_to(results).parts):
                    upload(prefix + "/results/" + str(path.relative_to(results)), path.read_bytes())
            with control._session() as db:
                rows = [dict(row) for row in db.execute("SELECT * FROM episodes WHERE experiment_id=?", (launch["experiment_id"],))]
            attempt_keys = {(a["episode_id"], a["attempt"]) for a in detail["attempts"]}
            for row in rows:
                if (row["episode_id"], row["attempt"]) not in attempt_keys:
                    continue
                for key in ("config", "events", "metrics", "final_state", "manifest", "release", "episode", "observability"):
                    if isinstance(row.get(key), str):
                        row[key] = json.loads(row[key])
                uid = row["episode_uid"]
                trace_key = upload(f"{prefix}/episodes/{uid}.json", json.dumps(row, sort_keys=True).encode())
                summary = {key: row.get(key) for key in ("episode_uid", "episode_id", "environment_id", "experiment_name", "cell_id", "episode_idx", "release_id", "item_id", "attempt", "seed", "status", "metrics", "started_at", "ended_at", "created_at")}
                summary.update(id=uid, owner_id=launch["owner_id"], experiment_id=launch["experiment_id"], launch_id=identifier, artifact_key=trace_key, artifact_json_key=trace_key)
                self.repository.put("episodes", uid, summary)
            for attempt in detail["attempts"]:
                attempt.update(owner_id=launch["owner_id"], experiment_id=launch["experiment_id"])
                self.repository.put("attempts", attempt["id"], attempt)
            detail_key = upload(prefix + "/detail.json", json.dumps(detail, sort_keys=True).encode())
            upload(prefix + "/checksums.json", json.dumps(checksums, sort_keys=True).encode())
            state = detail["launch"]
            # Missing scientific results must not become a successful launch.
            if any(a["status"] == "UNREPORTED" for a in detail["attempts"]):
                state.update(status="FAILED", error="runner completed without all planned evidence")
            self.repository.claim("launches", identifier, ACTIVE, {"status": state["status"], "ended_at": state.get("ended_at") or now(), "error": state.get("error"), "heartbeat_at": now(), "evidence_complete": True,
                "result_snapshot_key": prefix + "/trace.sqlite", "detail_key": detail_key,
                "artifacts_prefix": prefix + "/", "progress": detail["progress"]})


def run_once(repository=None, artifacts=None, launch_id=None, *, workspace=None):
    return Worker(repository, artifacts, workspace=workspace).run_once(launch_id)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--launch-id", default=os.getenv("PLATFORM_LAUNCH_ID"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    worker = Worker()
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    if args.launch_id:
        # Duplicate Cloud Run execution returns successfully without spending
        # model tokens twice. Platform failures exit nonzero; Job retries=0.
        try:
            worker.run_once(args.launch_id)
        finally:
            if worker.bridge:
                worker.bridge.shutdown()
        return 0
    while not worker.stopping.is_set():
        try:
            ran = worker.run_once()
        except Exception:
            import logging
            logging.exception("launch worker failed")
            ran = True
        if args.once:
            return 0
        if not ran:
            worker.stopping.wait(2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
