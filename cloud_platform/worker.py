"""Execute one admitted launch with its environment and agents colocated.

The immutable launch snapshot is input, not a live shared SQLite database.
Firestore receives small projections; the full evidence stays in artifacts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from cloud_platform.storage import ACTIVE, TERMINAL, ArtifactStore, Repository


def now():
    return datetime.now(UTC).isoformat()


class Worker:
    def __init__(self, repository=None, artifacts=None, *, workspace=None):
        self.repository = repository or Repository()
        self.artifacts = artifacts or ArtifactStore()
        self.workspace = Path(workspace or os.getenv("PLATFORM_WORKSPACE", "/workspace")).resolve()
        self.stopping = threading.Event()

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
                self._execute(claimed)
            except Exception as exc:
                # A retry must never re-execute an already claimed attempt.
                # Transient publication failures leave durable events intact.
                self.repository.claim("launches", claimed["id"], ACTIVE, {"status": "FAILED", "ended_at": now(), "error": str(exc), "evidence_complete": False})
                raise
            return True
        return False

    def _execute(self, launch):
        from local_stack.control_plane import ControlPlane

        identifier = launch["id"]
        prefix = f"launches/{identifier}"
        with tempfile.TemporaryDirectory(prefix="groundwork-launch-") as directory:
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
            control = ControlPlane.__new__(ControlPlane)
            control.path = database
            control.trace_database = database
            control.workspace = self.workspace
            control.results_dir = results
            control._init_db()
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
                       OTEL_TRACES_EXPORTER="file", A2A_CAPTURE_CONTENT="true")
            if self.artifacts.backend == "gcp":
                env.update(A2A_DURABLE_EVENT_SINK="cloud_platform.storage:durable_event_write",
                           A2A_DURABLE_EVENT_PREFIX=prefix + "/events")
            else:
                env.pop("A2A_DURABLE_EVENT_SINK", None)
                env.pop("A2A_DURABLE_EVENT_PREFIX", None)
            log_path = root / "runner.log"
            control._mark_launch_started(identifier)
            with log_path.open("w") as log:
                process = subprocess.Popen(command, cwd=self.workspace, env=env, stdout=log,
                                           stderr=subprocess.STDOUT, start_new_session=True)
                cancelled = False
                try:
                    while process.poll() is None:
                        record = self.repository.get("launches", identifier) or {}
                        if self.stopping.is_set() or record.get("status") == "CANCELLING" or record.get("cancel_requested"):
                            cancelled = record.get("status") == "CANCELLING" or bool(record.get("cancel_requested"))
                            os.killpg(process.pid, signal.SIGTERM)
                            try:
                                process.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                os.killpg(process.pid, signal.SIGKILL)
                            break
                        self.repository.claim("launches", identifier, {"RUNNING"}, {"heartbeat_at": now()})
                        self.stopping.wait(1)
                    code = process.wait()
                finally:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
            for line in log_path.read_text().splitlines():
                control._line(identifier, line)
            if cancelled:
                with control._session() as db:
                    db.execute("UPDATE launches SET status='CANCELLING' WHERE id=?", (identifier,))
            control._finish_launch(identifier, code)
            detail = control.launch_detail(identifier)
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
        worker.run_once(args.launch_id)
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
