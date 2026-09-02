"""Local, SQLite-backed experiment control plane.

The control plane deliberately does not accept source code or images from a
browser.  A researcher selects an installed ``GameRelease`` and an experiment
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

from a2a_engine.experiment import expand_batches, load_experiment
from a2a_engine.registry import get_game_spec, installed_games


_LIVE_RESULT = re.compile(r"INFO expt_runner: ok\s+(?P<episode>\S+)\s+->\s+(?P<uri>\S+)")
_SMOKE_RESULT = re.compile(
    r"^\s*OK\s+(?P<episode>\S+)\s+\[[^]]+\]\s+->\s+(?P<uri>\S+)"
)
_LIVE_FAILURE = re.compile(r"ERROR expt_runner: fail\s+(?P<episode>\S+):\s*(?P<error>.*)")
_SMOKE_FAILURE = re.compile(
    r"^\s*FAIL\s+(?P<episode>\S+)\s+\[[^]]+\]:\s*(?P<error>.*)"
)

# ``--smoke-test`` exercises the sink and the game wiring, so the runner
# executes this many runs per batch rather than the batch's declared count.
# The control plane must plan exactly what the runner will execute; otherwise
# it would record episode attempts that never ran.
SMOKE_RUNS_PER_BATCH = 1


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class GameRelease:
    id: str
    game_name: str
    package: str | None
    source_ref: str
    metadata: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class Experiment:
    id: str
    name: str
    game_name: str
    release_id: str
    yaml_path: str
    config_sha256: str
    created_at: str


@dataclass(frozen=True)
class Rollout:
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
class EpisodeAttempt:
    id: str
    rollout_id: str
    episode_id: str
    batch_label: str
    run_idx: int
    status: str
    trace_uri: str | None = None
    error: str | None = None
    started_at: str | None = None
    ended_at: str | None = None


class Launcher(Protocol):
    def launch(self, rollout: Rollout, experiment: Experiment, on_line, *, smoke_test: bool = False) -> None: ...

    def cancel(self, rollout_id: str) -> bool: ...


class LocalLauncher:
    """Start the existing runner as a bounded local child process."""

    def __init__(self, workspace: Path, control: "ControlPlane") -> None:
        self.workspace = workspace
        self.control = control
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._lock = threading.Lock()

    def launch(self, rollout: Rollout, experiment: Experiment, on_line, *, smoke_test: bool = False) -> None:
        command = [
            sys.executable,
            "-m",
            "expt_runner.run_experiment",
            experiment.yaml_path,
            "--storage-path",
            rollout.trace_database,
            "--max-parallelism",
            str(rollout.max_parallelism),
            # The workspace may be mounted read-only so host edits are live;
            # run artifacts belong beside the trace database regardless.
            "--results-dir",
            str(Path(rollout.trace_database).parent / "results"),
        ]
        if smoke_test:
            command += ["--smoke-test", "--smoke-runs-per-batch", str(SMOKE_RUNS_PER_BATCH)]
        env = dict(os.environ)
        env.setdefault("A2A_CAPTURE_CONTENT", "true")
        env.setdefault("OTEL_TRACES_EXPORTER", "file")
        env.setdefault(
            "A2A_OTEL_TRACES_FILE",
            str(Path(rollout.trace_database).with_name("otel-spans.jsonl")),
        )
        # A rollout uses one deterministic stream namespace; each runner
        # context appends its own episode suffix.  The URL stays outside all
        # persisted experiment/trace metadata.
        if env.get("A2A_REDIS_URL"):
            env["A2A_ROLLOUT_ID"] = rollout.id
            env["A2A_REDIS_STREAM_PREFIX"] = f"a2a:rollout:{rollout.id}"
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
            self._processes[rollout.id] = process
        self.control._mark_rollout_started(rollout.id)

        def watch() -> None:
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    on_line(line.rstrip())
                code = process.wait()
                self.control._finish_rollout(rollout.id, code)
            finally:
                with self._lock:
                    self._processes.pop(rollout.id, None)

        threading.Thread(target=watch, name=f"a2a-rollout-{rollout.id}", daemon=True).start()

    def cancel(self, rollout_id: str) -> bool:
        with self._lock:
            process = self._processes.get(rollout_id)
        if process is None or process.poll() is not None:
            return False
        process.terminate()
        return True


class ControlPlane:
    """Persistence, validation, and event log for local releases and rollouts."""

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
                CREATE TABLE IF NOT EXISTS game_releases (
                    id TEXT PRIMARY KEY, game_name TEXT NOT NULL UNIQUE,
                    package TEXT, source_ref TEXT NOT NULL, metadata TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, game_name TEXT NOT NULL,
                    release_id TEXT NOT NULL REFERENCES game_releases(id),
                    yaml_path TEXT NOT NULL, config_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rollouts (
                    id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES experiments(id),
                    status TEXT NOT NULL, max_parallelism INTEGER NOT NULL,
                    trace_database TEXT NOT NULL, created_at TEXT NOT NULL,
                    started_at TEXT, ended_at TEXT, error TEXT
                );
                CREATE TABLE IF NOT EXISTS episode_attempts (
                    id TEXT PRIMARY KEY, rollout_id TEXT NOT NULL REFERENCES rollouts(id),
                    episode_id TEXT NOT NULL, batch_label TEXT NOT NULL, run_idx INTEGER NOT NULL,
                    status TEXT NOT NULL, trace_uri TEXT, error TEXT,
                    started_at TEXT, ended_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_episode_attempt_unique
                    ON episode_attempts(rollout_id, episode_id);
                CREATE TABLE IF NOT EXISTS rollout_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rollout_id TEXT NOT NULL REFERENCES rollouts(id),
                    kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)

    def seed_installed_releases(self) -> list[GameRelease]:
        created: list[GameRelease] = []
        with self._connect() as db:
            # Only entry-point games are releases. A game registered directly at
            # runtime has no package behind it, so offering it as a launchable
            # release would present a release with nothing to run.
            for game_name in installed_games():
                spec = get_game_spec(game_name)
                row = db.execute(
                    "SELECT id FROM game_releases WHERE game_name = ?", (game_name,)
                ).fetchone()
                if row:
                    continue
                release = GameRelease(
                    id=f"local-{game_name}", game_name=game_name,
                    package=spec.package, source_ref="local-workspace",
                    metadata={"launcher": "local", "entrypoint": game_name}, created_at=_now(),
                )
                db.execute(
                    "INSERT INTO game_releases VALUES (?, ?, ?, ?, ?, ?)",
                    (release.id, release.game_name, release.package, release.source_ref,
                     _json(release.metadata), release.created_at),
                )
                created.append(release)
        return created

    def releases(self) -> list[GameRelease]:
        self.seed_installed_releases()
        with self._connect() as db:
            rows = db.execute("SELECT * FROM game_releases ORDER BY game_name").fetchall()
        return [self._release(row) for row in rows]

    def available_experiments(self) -> dict[str, list[str]]:
        """Checked-in experiment YAMLs, grouped by the game each one declares.

        The declared game is read from the file rather than inferred from its
        directory, so a release is never offered a config it cannot run.
        """
        found: dict[str, list[str]] = {}
        for path in sorted(self.workspace.glob("games/*/experiments/*.y*ml")):
            try:
                names = load_experiment(path).game_names()
            except Exception:
                # A malformed or half-written experiment must not take down the
                # release listing; it simply is not offered.
                continue
            if len(names) != 1:
                continue
            found.setdefault(names[0], []).append(str(path.relative_to(self.workspace)))
        # Surface the small credential-free configs first. A game may ship
        # dozens of research configs, and the one a newcomer should open is the
        # worked example, not whichever sorts first alphabetically.
        def rank(path: str) -> tuple[int, str]:
            stem = Path(path).stem
            if "smoke" in stem:
                return (0, stem)
            if "example" in stem:
                return (1, stem)
            return (2, stem)

        return {game: sorted(paths, key=rank) for game, paths in found.items()}

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

        A live rollout fails deep inside an HTTP client when a key is missing.
        Reporting the line-up and its credential state before launch is what
        makes that failure avoidable rather than merely explainable.
        """
        from a2a_engine.llm.factory import (
            detect_provider, env_var_for_provider, get_api_key_for_provider,
        )

        path = self._workspace_path(yaml_path)
        spec = load_experiment(path)
        game_names = spec.game_names()
        resolve_hooks = {
            name: get_game_spec(name).resolve_config
            for name in game_names if name
        }

        agents: list[dict[str, Any]] = []
        seen: set[str] = set()
        declares_agents = False
        for batch, resolved in expand_batches(spec, resolve_config=resolve_hooks):
            if resolved.get("agents"):
                declares_agents = True
            for index, entry in enumerate(resolved.get("agents") or []):
                agent = entry if isinstance(entry, dict) else entry.model_dump()
                model = agent.get("model") or None
                signature = _json([batch.label, index, agent.get("type"), model])
                if signature in seen:
                    continue
                seen.add(signature)

                provider = detect_provider(model) if model else None
                env_var = env_var_for_provider(provider) if provider else ""
                agents.append({
                    "batch_label": batch.label,
                    "index": index,
                    "type": agent.get("type") or "llm",
                    "model": model,
                    "api_format": agent.get("api_format"),
                    "provider": provider,
                    # A scripted agent needs no credential; an ADC provider
                    # needs one but not from the environment.
                    "credential_env_var": env_var or None,
                    "credential_present": bool(get_api_key_for_provider(provider)) if env_var else None,
                })

        missing = sorted({
            agent["credential_env_var"] for agent in agents
            if agent["credential_env_var"] and not agent["credential_present"]
        })
        # A config that names no agents leaves the line-up to the game's own
        # defaults, which are chosen at construction time. Reporting that as
        # ready would assert a credential state nothing here has checked.
        return {
            "agents": agents,
            "missing_credentials": missing,
            "declares_agents": declares_agents,
            "ready_for_live": (not missing) if declares_agents else None,
            "readiness_note": None if declares_agents else (
                "This configuration does not declare agents, so the game supplies "
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
        game_names = spec.game_names()
        if len(game_names) != 1:
            raise ValueError("a control-plane experiment must select exactly one game")
        game_name = game_names[0]
        release = self._release_for(game_name, release_id)
        experiment = Experiment(
            id=str(uuid.uuid4()), name=name or spec.name, game_name=game_name,
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

    def launch_rollout(self, experiment_id: str, *, max_parallelism: int = 1,
                       smoke_test: bool = False) -> Rollout:
        if max_parallelism < 1:
            raise ValueError("max_parallelism must be >= 1")
        experiment = self.experiment(experiment_id)
        attempts = self._planned_attempts(experiment, smoke_test=smoke_test)
        rollout = Rollout(
            id=str(uuid.uuid4()), experiment_id=experiment.id, status="QUEUED",
            max_parallelism=max_parallelism, trace_database=str(self.trace_database),
            created_at=_now(),
        )
        with self._connect() as db:
            db.execute(
                "INSERT INTO rollouts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(asdict(rollout).values()),
            )
            db.executemany(
                "INSERT INTO episode_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (str(uuid.uuid4()), rollout.id, episode_id, batch_label, run_idx,
                     "QUEUED", None, None, None, None)
                    for episode_id, batch_label, run_idx in attempts
                ],
            )
            self._event(db, rollout.id, "rollout.queued", {
                "episode_count": len(attempts), "mode": "smoke" if smoke_test else "live",
            })
        self.launcher.launch(
            rollout, experiment, lambda line: self._line(rollout.id, line), smoke_test=smoke_test,
        )
        return self.rollout(rollout.id)

    def cancel_rollout(self, rollout_id: str) -> Rollout:
        rollout = self.rollout(rollout_id)
        if rollout.status not in {"QUEUED", "RUNNING"}:
            return rollout
        if self.launcher.cancel(rollout_id):
            with self._connect() as db:
                db.execute("UPDATE rollouts SET status = ? WHERE id = ?", ("CANCELLING", rollout_id))
                self._event(db, rollout_id, "rollout.cancelling", {})
        return self.rollout(rollout_id)

    def rollout(self, rollout_id: str) -> Rollout:
        with self._connect() as db:
            row = db.execute("SELECT * FROM rollouts WHERE id = ?", (rollout_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown rollout {rollout_id}")
        return self._rollout(row)

    def rollout_detail(self, rollout_id: str) -> dict[str, Any]:
        rollout = self.rollout(rollout_id)
        with self._connect() as db:
            attempts = db.execute(
                "SELECT * FROM episode_attempts WHERE rollout_id = ? ORDER BY batch_label, run_idx",
                (rollout_id,),
            ).fetchall()
        prefix = f"a2a:rollout:{rollout.id}:episode:"
        return {
            "rollout": asdict(rollout),
            "episode_attempts": [
                {**asdict(self._attempt(row)), "redis_stream": prefix + row["episode_id"]}
                for row in attempts
            ],
        }

    def rollouts(self) -> list[Rollout]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM rollouts ORDER BY created_at DESC").fetchall()
        return [self._rollout(row) for row in rows]

    def events(self, rollout_id: str, *, after_id: int = 0) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM rollout_events WHERE rollout_id = ? AND id > ? ORDER BY id",
                (rollout_id, after_id),
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
        resolved_hooks = {experiment.game_name: get_game_spec(experiment.game_name).resolve_config}
        attempts: list[tuple[str, str, int]] = []
        for batch, _ in expand_batches(spec, resolve_config=resolved_hooks):
            count = SMOKE_RUNS_PER_BATCH if smoke_test else batch.count
            for run_idx in range(count):
                attempts.append((f"{spec.name}.{batch.label}.{run_idx}", batch.label, run_idx))
        return attempts

    def _workspace_path(self, user_path: str) -> Path:
        candidate = (self.workspace / user_path).resolve()
        if self.workspace not in candidate.parents and candidate != self.workspace:
            raise ValueError("yaml_path must stay within the workspace")
        return candidate

    def _release_for(self, game_name: str, release_id: str | None) -> GameRelease:
        self.seed_installed_releases()
        with self._connect() as db:
            if release_id:
                row = db.execute("SELECT * FROM game_releases WHERE id = ?", (release_id,)).fetchone()
                if row is None:
                    # An unregistered release and a mismatched one are different
                    # problems: the first usually means the game never made it
                    # into the registry at all, and naming the known releases
                    # says so directly.
                    known = [r["id"] for r in db.execute(
                        "SELECT id FROM game_releases ORDER BY id").fetchall()]
                    raise ValueError(
                        f"no release registered as {release_id!r}; installed releases: {known}"
                    )
                if row["game_name"] != game_name:
                    raise ValueError(
                        f"release {release_id!r} is for game {row['game_name']!r}, "
                        f"but the experiment declares {game_name!r}"
                    )
            else:
                row = db.execute("SELECT * FROM game_releases WHERE game_name = ?", (game_name,)).fetchone()
        if row is None:
            raise ValueError(f"no local release registered for game {game_name!r}")
        return self._release(row)

    def _line(self, rollout_id: str, line: str) -> None:
        if not line:
            return
        with self._connect() as db:
            self._event(db, rollout_id, "runner.log", {"line": line})
            result = _LIVE_RESULT.search(line) or _SMOKE_RESULT.search(line)
            if result:
                identifier, uri = result["episode"], result["uri"]
                now = _now()
                db.execute(
                    "UPDATE episode_attempts SET status = ?, trace_uri = ?, ended_at = ? "
                    "WHERE rollout_id = ? AND episode_id = ?",
                    ("COMPLETED", uri, now, rollout_id, identifier),
                )
                self._event(db, rollout_id, "episode.completed", {"episode_id": identifier, "trace_uri": uri})
                return
            # A failed episode reports its own diagnostic. Attributing it to
            # that attempt keeps the per-episode error out of the rollout-wide
            # exit status, which cannot say which run broke.
            failure = _LIVE_FAILURE.search(line) or _SMOKE_FAILURE.search(line)
            if failure:
                identifier, error = failure["episode"], failure["error"].strip()
                db.execute(
                    "UPDATE episode_attempts SET status = ?, error = ?, ended_at = ? "
                    "WHERE rollout_id = ? AND episode_id = ?",
                    ("FAILED", error, _now(), rollout_id, identifier),
                )
                self._event(db, rollout_id, "episode.failed", {"episode_id": identifier, "error": error})

    def _mark_rollout_started(self, rollout_id: str) -> None:
        now = _now()
        with self._connect() as db:
            db.execute("UPDATE rollouts SET status = ?, started_at = ? WHERE id = ?", ("RUNNING", now, rollout_id))
            db.execute("UPDATE episode_attempts SET status = ?, started_at = ? WHERE rollout_id = ? AND status = ?", ("RUNNING", now, rollout_id, "QUEUED"))
            self._event(db, rollout_id, "rollout.started", {})

    def _finish_rollout(self, rollout_id: str, code: int) -> None:
        with self._connect() as db:
            current = db.execute("SELECT status FROM rollouts WHERE id = ?", (rollout_id,)).fetchone()
            cancelled = current and current["status"] == "CANCELLING"
            status = "CANCELLED" if cancelled else ("COMPLETED" if code == 0 else "FAILED")
            error = None if code == 0 or cancelled else f"runner exited with status {code}"
            now = _now()
            db.execute("UPDATE rollouts SET status = ?, ended_at = ?, error = ? WHERE id = ?", (status, now, error, rollout_id))
            if code == 0 and not cancelled:
                # The runner exits zero only after every scheduled run
                # succeeded, so a still-RUNNING attempt means the CLI never
                # reported that episode. Recording it as COMPLETED would
                # invent a result and a missing trace URI; UNREPORTED keeps
                # the gap visible instead.
                unreported = [
                    row["episode_id"]
                    for row in db.execute(
                        "SELECT episode_id FROM episode_attempts WHERE rollout_id = ? AND status = ?",
                        (rollout_id, "RUNNING"),
                    ).fetchall()
                ]
                if unreported:
                    db.execute(
                        "UPDATE episode_attempts SET status = ?, ended_at = ?, error = ? "
                        "WHERE rollout_id = ? AND status = ?",
                        ("UNREPORTED", now, "runner exited 0 without reporting this episode",
                         rollout_id, "RUNNING"),
                    )
                    self._event(db, rollout_id, "rollout.unreported_episodes",
                                {"episode_ids": unreported})
            else:
                db.execute(
                    "UPDATE episode_attempts SET status = ?, ended_at = ?, error = ? "
                    "WHERE rollout_id = ? AND status = ?",
                    ("CANCELLED" if cancelled else "FAILED", now, error, rollout_id, "RUNNING"),
                )
            self._event(db, rollout_id, f"rollout.{status.lower()}", {"exit_code": code, "error": error})

    @staticmethod
    def _event(db: sqlite3.Connection, rollout_id: str, kind: str, payload: dict[str, Any]) -> None:
        db.execute(
            "INSERT INTO rollout_events (rollout_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (rollout_id, kind, _json(payload), _now()),
        )

    @staticmethod
    def _release(row: sqlite3.Row) -> GameRelease:
        return GameRelease(row["id"], row["game_name"], row["package"], row["source_ref"], json.loads(row["metadata"]), row["created_at"])

    @staticmethod
    def _experiment(row: sqlite3.Row) -> Experiment:
        return Experiment(**dict(row))

    @staticmethod
    def _rollout(row: sqlite3.Row) -> Rollout:
        return Rollout(**dict(row))

    @staticmethod
    def _attempt(row: sqlite3.Row) -> EpisodeAttempt:
        return EpisodeAttempt(**dict(row))
