"""Local, SQLite-backed experiment control plane.

The control plane deliberately does not accept source code or images from a
browser.  A researcher selects an installed ``Release`` and an experiment
YAML already present in the checked-out workspace; the existing ``a2a-run``
contract remains the only episode executor.  The same records and ``Launcher``
boundary are intended to be reused by a later Cloud Run implementation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from a2a_engine.experiment import expand_cells, load_experiment
from a2a_engine.registry import get_environment_spec, installed_environments


_LIVE_RESULT = re.compile(r"INFO expt_runner: ok\s+(?P<episode>\S+)\s+->\s+(?P<uri>\S+)")
_SMOKE_RESULT = re.compile(
    r"^\s*OK\s+(?P<episode>\S+)\s+\[[^]]+\]\s+->\s+(?P<uri>\S+)"
)
_LIVE_FAILURE = re.compile(r"ERROR expt_runner: fail\s+(?P<episode>\S+):\s*(?P<error>.*)")
_SMOKE_FAILURE = re.compile(
    r"^\s*FAIL\s+(?P<episode>\S+)\s+\[[^]]+\]:\s*(?P<error>.*)"
)

# ``--smoke-test`` exercises the sink and the environment wiring, so the runner
# executes this many runs per cell rather than the cell's declared count.
# The control plane must plan exactly what the runner will execute; otherwise
# it would record episode attempts that never ran.
SMOKE_EPISODES_PER_CELL = 1


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class Release:
    id: str
    environment_id: str
    package: str | None
    source_ref: str
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class Experiment:
    id: str
    name: str
    environment_id: str
    release_id: str
    yaml_path: str
    config_sha256: str
    created_at: str


@dataclass(frozen=True)
class Launch:
    id: str
    experiment_id: str
    status: str
    max_parallelism: int
    trace_database: str
    created_at: str
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class Attempt:
    id: str
    launch_id: str
    episode_id: str
    cell_id: str
    episode_idx: int
    status: str
    episode_uri: str | None = None
    error: str | None = None
    started_at: str | None = None
    ended_at: str | None = None


class Launcher(Protocol):
    def launch(self, launch: Launch, experiment: Experiment, on_line, *, smoke_test: bool = False) -> None: ...

    def cancel(self, launch_id: str) -> bool: ...


class LocalLauncher:
    """Start the existing runner as a bounded local child process."""

    def __init__(self, workspace: Path, control: "ControlPlane") -> None:
        self.workspace = workspace
        self.control = control
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._lock = threading.Lock()

    def launch(self, launch: Launch, experiment: Experiment, on_line, *, smoke_test: bool = False) -> None:
        command = [
            sys.executable,
            "-m",
            "expt_runner.run_experiment",
            experiment.yaml_path,
            "--storage-path",
            launch.trace_database,
            "--max-parallelism",
            str(launch.max_parallelism),
            # The workspace may be mounted read-only so host edits are live;
            # run artifacts belong beside the trace database regardless.
            "--results-dir",
            str(Path(launch.trace_database).parent / "results"),
        ]
        if smoke_test:
            command += ["--smoke-test", "--smoke-episodes-per-cell", str(SMOKE_EPISODES_PER_CELL)]
        env = dict(os.environ)
        env.setdefault("A2A_CAPTURE_CONTENT", "true")
        env.setdefault("OTEL_TRACES_EXPORTER", "file")
        env.setdefault(
            "A2A_OTEL_TRACES_FILE",
            str(Path(launch.trace_database).with_name("otel-spans.jsonl")),
        )
        # A launch uses one deterministic stream namespace; each runner
        # context appends its own episode suffix.  The URL stays outside all
        # persisted experiment/trace metadata.
        if env.get("A2A_REDIS_URL"):
            env["A2A_LAUNCH_ID"] = launch.id
            env["A2A_REDIS_STREAM_PREFIX"] = f"a2a:launch:{launch.id}"
        process = subprocess.Popen(
            command,
            cwd=self.workspace,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        with self._lock:
            self._processes[launch.id] = process
        self.control._mark_launch_started(launch.id)

        def watch() -> None:
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    on_line(line.rstrip())
                code = process.wait()
                self.control._finish_launch(launch.id, code)
            finally:
                with self._lock:
                    self._processes.pop(launch.id, None)

        threading.Thread(target=watch, name=f"a2a-launch-{launch.id}", daemon=True).start()

    def cancel(self, launch_id: str) -> bool:
        with self._lock:
            process = self._processes.get(launch_id)
        if process is None or process.poll() is not None:
            return False
        process.terminate()
        return True


class ControlPlane:
    """Persistence, validation, and event log for local releases and launches."""

    def __init__(self, path: str | Path, *, workspace: str | Path,
                 trace_database: str | Path) -> None:
        self.path = Path(path)
        self.workspace = Path(workspace).resolve()
        self.trace_database = Path(trace_database).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.launcher: Launcher = LocalLauncher(self.workspace, self)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as db:
            db.executescript("""
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS releases (
                    id TEXT PRIMARY KEY, environment_id TEXT NOT NULL UNIQUE,
                    package TEXT, source_ref TEXT NOT NULL, metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, environment_id TEXT NOT NULL,
                    release_id TEXT NOT NULL REFERENCES releases(id),
                    yaml_path TEXT NOT NULL, config_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS launches (
                    id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES experiments(id),
                    status TEXT NOT NULL, max_parallelism INTEGER NOT NULL,
                    trace_database TEXT NOT NULL, created_at TEXT NOT NULL,
                    started_at TEXT, ended_at TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, launch_id TEXT NOT NULL REFERENCES launches(id),
                    episode_id TEXT NOT NULL, cell_id TEXT NOT NULL, episode_idx INTEGER NOT NULL,
                    status TEXT NOT NULL, episode_uri TEXT, error TEXT,
                    started_at TEXT, ended_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_episode_attempt_unique
                    ON attempts(launch_id, episode_id);
                CREATE TABLE IF NOT EXISTS launch_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    launch_id TEXT NOT NULL REFERENCES launches(id),
                    kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)

    def seed_installed_releases(self) -> list[Release]:
        created: list[Release] = []
        with self._connect() as db:
            # Only entry-point games are releases. A environment registered directly at
            # runtime has no package behind it, so offering it as a launchable
            # release would present a release with nothing to run.
            for environment_id in installed_environments():
                spec = get_environment_spec(environment_id)
                row = db.execute(
                    "SELECT id FROM releases WHERE environment_id = ?", (environment_id,)
                ).fetchone()
                if row:
                    continue
                release = Release(
                    id=f"local-{environment_id}", environment_id=environment_id,
                    package=spec.package, source_ref="local-workspace",
                    metadata={"launcher": "local", "entrypoint": environment_id}, created_at=_now(),
                )
                db.execute(
                    "INSERT INTO releases VALUES (?, ?, ?, ?, ?, ?)",
                    (release.id, release.environment_id, release.package, release.source_ref,
                     _json(release.metadata), release.created_at),
                )
                created.append(release)
        return created

    def releases(self) -> list[Release]:
        self.seed_installed_releases()
        with self._connect() as db:
            rows = db.execute("SELECT * FROM releases ORDER BY environment_id").fetchall()
        return [self._release(row) for row in rows]

    def available_experiments(self) -> dict[str, list[str]]:
        """Checked-in experiment YAMLs, grouped by the environment each one declares.

        The declared environment is read from the file rather than inferred from its
        directory, so a release is never offered a config it cannot run.
        """
        found: dict[str, list[str]] = {}
        for path in sorted(self.workspace.glob("games/*/experiments/*.y*ml")):
            try:
                names = load_experiment(path).environment_ids()
            except Exception:
                # A malformed or half-written experiment must not take down the
                # release listing; it simply is not offered.
                continue
            if len(names) != 1:
                continue
            found.setdefault(names[0], []).append(str(path.relative_to(self.workspace)))
        # Surface the small credential-free configs first. A environment may ship
        # dozens of research configs, and the one a newcomer should open is the
        # worked example, not whichever sorts first alphabetically.
        def rank(path: str) -> tuple[int, str]:
            stem = Path(path).stem
            if "smoke" in stem:
                return (0, stem)
            if "example" in stem:
                return (1, stem)
            return (2, stem)

        return {environment: sorted(paths, key=rank) for environment, paths in found.items()}

    def agent_pool(self) -> dict[str, Any]:
        """The effective agent pool and whether each binding can run here."""
        from a2a_engine.agent_pool import load_agent_pool

        pool = load_agent_pool(self.workspace / "experiments")
        entries = []
        for name, entry in sorted(pool.agents.items()):
            entries.append({
                "name": name,
                "description": entry.description,
                "type": entry.type,
                "model": entry.model,
                "api_format": entry.api_format,
                "api_base": entry.api_base,
                "credential": entry.credential,
                "credential_present": (
                    bool(os.environ.get(entry.credential)) if entry.credential else None
                ),
            })
        return {"agents": entries, "sources": pool.sources}

    def read_experiment_config(self, yaml_path: str) -> dict[str, Any]:
        """Return the text of a checked-in experiment config, for review.

        Only configs this control plane already offers are readable. Serving
        any workspace file that merely ends in .yaml would turn a viewer
        convenience into an arbitrary file reader.
        """
        offered = {path for paths in self.available_experiments().values() for path in paths}
        if yaml_path not in offered:
            raise ValueError(f"{yaml_path!r} is not an offered experiment configuration")
        path = self._workspace_path(yaml_path)
        return {
            "yaml_path": yaml_path,
            "sha256": _digest(path),
            "content": path.read_text(encoding="utf-8"),
        }

    def experiment_agents(self, yaml_path: str) -> dict[str, Any]:
        """The agents a config will actually run, and whether they can run.

        A live launch fails deep inside an HTTP client when a key is missing.
        Reporting the line-up and its credential state before launch is what
        makes that failure avoidable rather than merely explainable.
        """
        from a2a_engine.llm.factory import (
            detect_provider, env_var_for_provider, get_api_key_for_provider,
        )

        path = self._workspace_path(yaml_path)
        spec = load_experiment(path)
        environment_ids = spec.environment_ids()
        resolve_hooks = {
            name: get_environment_spec(name).resolve_config
            for name in environment_ids if name
        }

        agents: list[dict[str, Any]] = []
        seen: set[str] = set()
        declares_agents = False
        for cell, resolved in expand_cells(spec, resolve_config=resolve_hooks):
            if resolved.get("agents"):
                declares_agents = True
            for index, entry in enumerate(resolved.get("agents") or []):
                agent = entry if isinstance(entry, dict) else entry.model_dump()
                model = agent.get("model") or None
                signature = _json([cell.label, index, agent.get("type"), model])
                if signature in seen:
                    continue
                seen.add(signature)

                provider = detect_provider(model) if model else None
                env_var = env_var_for_provider(provider) if provider else ""
                agents.append({
                    "cell_id": cell.label,
                    "index": index,
                    "type": agent.get("type") or "llm",
                    "model": model,
                    "api_format": agent.get("api_format"),
                    "provider": provider,
                    # A scripted agent needs no credential; an ADC provider
                    # needs one but not from the release.
                    "credential_env_var": env_var or None,
                    "credential_present": bool(get_api_key_for_provider(provider)) if env_var else None,
                })

        missing = sorted({
            agent["credential_env_var"] for agent in agents
            if agent["credential_env_var"] and not agent["credential_present"]
        })
        # A config that names no agents leaves the line-up to the environment's own
        # defaults, which are chosen at construction time. Reporting that as
        # ready would assert a credential state nothing here has checked.
        return {
            "agents": agents,
            "missing_credentials": missing,
            "declares_agents": declares_agents,
            "ready_for_live": (not missing) if declares_agents else None,
            "readiness_note": None if declares_agents else (
                "This configuration does not declare agents, so the environment supplies "
                "its own defaults and the models it will call cannot be checked "
                "before launch."
            ),
        }

    def create_experiment(self, *, yaml_path: str, release_id: str | None = None,
                          name: str | None = None) -> Experiment:
        path = self._workspace_path(yaml_path)
        if not path.is_file() or path.suffix not in {".yaml", ".yml"}:
            raise ValueError("yaml_path must identify an experiment YAML within the workspace")
        spec = load_experiment(path)
        environment_ids = spec.environment_ids()
        if len(environment_ids) != 1:
            raise ValueError("a control-plane experiment must select exactly one environment")
        environment_id = environment_ids[0]
        release = self._release_for(environment_id, release_id)
        experiment = Experiment(
            id=str(uuid.uuid4()), name=name or spec.name, environment_id=environment_id,
            release_id=release.id, yaml_path=str(path.relative_to(self.workspace)),
            config_sha256=_digest(path), created_at=_now(),
        )
        with self._connect() as db:
            db.execute(
                "INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?)",
                tuple(asdict(experiment).values()),
            )
        return experiment

    def experiments(self) -> list[Experiment]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM experiments ORDER BY created_at DESC").fetchall()
        return [self._experiment(row) for row in rows]

    def launch_experiment(self, experiment_id: str, *, max_parallelism: int = 1,
                       smoke_test: bool = False) -> Launch:
        if max_parallelism < 1:
            raise ValueError("max_parallelism must be >= 1")
        experiment = self.experiment(experiment_id)
        attempts = self._planned_attempts(experiment, smoke_test=smoke_test)
        launch = Launch(
            id=str(uuid.uuid4()), experiment_id=experiment.id, status="QUEUED",
            max_parallelism=max_parallelism, trace_database=str(self.trace_database),
            created_at=_now(),
        )
        with self._connect() as db:
            db.execute(
                "INSERT INTO launches VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(asdict(launch).values()),
            )
            db.executemany(
                "INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (str(uuid.uuid4()), launch.id, episode_id, cell_id, episode_idx,
                     "QUEUED", None, None, None, None)
                    for episode_id, cell_id, episode_idx in attempts
                ],
            )
            self._event(db, launch.id, "launch.queued", {
                "episode_count": len(attempts), "mode": "smoke" if smoke_test else "live",
            })
        self.launcher.launch(
            launch, experiment, lambda line: self._line(launch.id, line), smoke_test=smoke_test,
        )
        return self.launch(launch.id)

    def cancel_launch(self, launch_id: str) -> Launch:
        launch = self.launch(launch_id)
        if launch.status not in {"QUEUED", "RUNNING"}:
            return launch
        if self.launcher.cancel(launch_id):
            with self._connect() as db:
                db.execute("UPDATE launches SET status = ? WHERE id = ?", ("CANCELLING", launch_id))
                self._event(db, launch_id, "launch.cancelling", {})
        return self.launch(launch_id)

    def launch(self, launch_id: str) -> Launch:
        with self._connect() as db:
            row = db.execute("SELECT * FROM launches WHERE id = ?", (launch_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown launch {launch_id}")
        return self._launch(row)

    def launch_detail(self, launch_id: str) -> dict[str, Any]:
        launch = self.launch(launch_id)
        with self._connect() as db:
            attempts = db.execute(
                "SELECT * FROM attempts WHERE launch_id = ? ORDER BY cell_id, episode_idx",
                (launch_id,),
            ).fetchall()
        prefix = f"a2a:launch:{launch.id}:episode:"
        return {
            "launch": asdict(launch),
            "attempts": [
                {**asdict(self._attempt(row)), "redis_stream": prefix + row["episode_id"]}
                for row in attempts
            ],
        }

    def launches(self) -> list[Launch]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM launches ORDER BY created_at DESC").fetchall()
        return [self._launch(row) for row in rows]

    def events(self, launch_id: str, *, after_id: int = 0) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM launch_events WHERE launch_id = ? AND id > ? ORDER BY id",
                (launch_id, after_id),
            ).fetchall()
        return [
            {"id": row["id"], "kind": row["kind"], "payload": json.loads(row["payload"]),
             "created_at": row["created_at"]}
            for row in rows
        ]

    def experiment(self, experiment_id: str) -> Experiment:
        with self._connect() as db:
            row = db.execute("SELECT * FROM experiments WHERE id = ?", (experiment_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown experiment {experiment_id}")
        return self._experiment(row)

    def _planned_attempts(self, experiment: Experiment, *,
                          smoke_test: bool = False) -> list[tuple[str, str, int]]:
        path = self._workspace_path(experiment.yaml_path)
        spec = load_experiment(path)
        resolved_hooks = {experiment.environment_id: get_environment_spec(experiment.environment_id).resolve_config}
        attempts: list[tuple[str, str, int]] = []
        for cell, _ in expand_cells(spec, resolve_config=resolved_hooks):
            count = SMOKE_EPISODES_PER_CELL if smoke_test else cell.count
            for episode_idx in range(count):
                attempts.append((f"{spec.name}.{cell.label}.{episode_idx}", cell.label, episode_idx))
        return attempts

    def _workspace_path(self, user_path: str) -> Path:
        candidate = (self.workspace / user_path).resolve()
        if self.workspace not in candidate.parents and candidate != self.workspace:
            raise ValueError("yaml_path must stay within the workspace")
        return candidate

    def _release_for(self, environment_id: str, release_id: str | None) -> Release:
        self.seed_installed_releases()
        with self._connect() as db:
            if release_id:
                row = db.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
                if row is None:
                    # An unregistered release and a mismatched one are different
                    # problems: the first usually means the environment never made it
                    # into the registry at all, and naming the known releases
                    # says so directly.
                    known = [r["id"] for r in db.execute(
                        "SELECT id FROM releases ORDER BY id").fetchall()]
                    raise ValueError(
                        f"no release registered as {release_id!r}; installed releases: {known}"
                    )
                if row["environment_id"] != environment_id:
                    raise ValueError(
                        f"release {release_id!r} is for environment {row['environment_id']!r}, "
                        f"but the experiment declares {environment_id!r}"
                    )
            else:
                row = db.execute("SELECT * FROM releases WHERE environment_id = ?", (environment_id,)).fetchone()
        if row is None:
            raise ValueError(f"no local release registered for environment {environment_id!r}")
        return self._release(row)

    def _line(self, launch_id: str, line: str) -> None:
        if not line:
            return
        with self._connect() as db:
            self._event(db, launch_id, "runner.log", {"line": line})
            result = _LIVE_RESULT.search(line) or _SMOKE_RESULT.search(line)
            if result:
                identifier, uri = result["episode"], result["uri"]
                now = _now()
                db.execute(
                    "UPDATE attempts SET status = ?, episode_uri = ?, ended_at = ? "
                    "WHERE launch_id = ? AND episode_id = ?",
                    ("COMPLETED", uri, now, launch_id, identifier),
                )
                self._event(db, launch_id, "episode.completed", {"episode_id": identifier, "episode_uri": uri})
                return
            # A failed episode reports its own diagnostic. Attributing it to
            # that attempt keeps the per-episode error out of the launch-wide
            # exit status, which cannot say which run broke.
            failure = _LIVE_FAILURE.search(line) or _SMOKE_FAILURE.search(line)
            if failure:
                identifier, error = failure["episode"], failure["error"].strip()
                db.execute(
                    "UPDATE attempts SET status = ?, error = ?, ended_at = ? "
                    "WHERE launch_id = ? AND episode_id = ?",
                    ("FAILED", error, _now(), launch_id, identifier),
                )
                self._event(db, launch_id, "episode.failed", {"episode_id": identifier, "error": error})

    def _mark_launch_started(self, launch_id: str) -> None:
        now = _now()
        with self._connect() as db:
            db.execute("UPDATE launches SET status = ?, started_at = ? WHERE id = ?", ("RUNNING", now, launch_id))
            db.execute("UPDATE attempts SET status = ?, started_at = ? WHERE launch_id = ? AND status = ?", ("RUNNING", now, launch_id, "QUEUED"))
            self._event(db, launch_id, "launch.started", {})

    def _finish_launch(self, launch_id: str, code: int) -> None:
        with self._connect() as db:
            current = db.execute("SELECT status FROM launches WHERE id = ?", (launch_id,)).fetchone()
            cancelled = current and current["status"] == "CANCELLING"
            status = "CANCELLED" if cancelled else ("COMPLETED" if code == 0 else "FAILED")
            error = None if code == 0 or cancelled else f"runner exited with status {code}"
            now = _now()
            db.execute("UPDATE launches SET status = ?, ended_at = ?, error = ? WHERE id = ?", (status, now, error, launch_id))
            if code == 0 and not cancelled:
                # The runner exits zero only after every scheduled run
                # succeeded, so a still-RUNNING attempt means the CLI never
                # reported that episode. Recording it as COMPLETED would
                # invent a result and a missing trace URI; UNREPORTED keeps
                # the gap visible instead.
                unreported = [
                    row["episode_id"]
                    for row in db.execute(
                        "SELECT episode_id FROM attempts WHERE launch_id = ? AND status = ?",
                        (launch_id, "RUNNING"),
                    ).fetchall()
                ]
                if unreported:
                    db.execute(
                        "UPDATE attempts SET status = ?, ended_at = ?, error = ? "
                        "WHERE launch_id = ? AND status = ?",
                        ("UNREPORTED", now, "runner exited 0 without reporting this episode",
                         launch_id, "RUNNING"),
                    )
                    self._event(db, launch_id, "launch.unreported_episodes",
                                {"episode_ids": unreported})
            else:
                db.execute(
                    "UPDATE attempts SET status = ?, ended_at = ?, error = ? "
                    "WHERE launch_id = ? AND status = ?",
                    ("CANCELLED" if cancelled else "FAILED", now, error, launch_id, "RUNNING"),
                )
            self._event(db, launch_id, f"launch.{status.lower()}", {"exit_code": code, "error": error})

    @staticmethod
    def _event(db: sqlite3.Connection, launch_id: str, kind: str, payload: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO launch_events (launch_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (launch_id, kind, _json(payload), _now()),
        )

    @staticmethod
    def _release(row: sqlite3.Row) -> Release:
        return Release(row["id"], row["environment_id"], row["package"], row["source_ref"], json.loads(row["metadata"]), row["created_at"])

    @staticmethod
    def _experiment(row: sqlite3.Row) -> Experiment:
        return Experiment(**dict(row))

    @staticmethod
    def _launch(row: sqlite3.Row) -> Launch:
        return Launch(**dict(row))

    @staticmethod
    def _attempt(row: sqlite3.Row) -> Attempt:
        return Attempt(**dict(row))
