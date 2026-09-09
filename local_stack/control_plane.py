"""Local, SQLite-backed experiment control plane.

The control plane deliberately does not accept source code or images from a
browser.  A researcher selects an installed ``Release`` and an experiment
YAML already present in the checked-out workspace; the existing ``a2a-run``
contract remains the only episode executor.  The same records and ``Launcher``
boundary are intended to be reused by a later Cloud Run implementation.

It shares **one database** with the episode store (DDL in
``a2a_engine.storage.schema``), which is what makes progress an identity join
in SQL rather than a regex over the runner's stdout: the entire fan-out is
materialised at launch time and ``episode_id`` is deterministic, so "which of
these planned episodes exist" is a question the database can answer.  Stdout
parsing survives only as a latency hint for the live event feed.

Because that join needs no live process handle, reconciling after a restart
follows for free: a launch whose runner died is resolved from the same query,
and an attempt with no episode row is looked for in the durable event log
before it is written off.
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
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator, Protocol

from a2a_engine.compiler import ExecutionPlan as CompiledExecutionPlan
from a2a_engine.compiler import compile as compile_design
from a2a_engine.compiler import validate as validate_design
from a2a_engine.design import DesignValidationError, ValidationIssue, parse_design_text
from a2a_engine.event_sink import iter_event_sinks, read_event_sink
from a2a_engine.experiment import expand_cells, load_experiment
from a2a_engine.items import ItemBank, derive_item_domain
from a2a_engine.manifest import EpisodeManifest
from a2a_engine.registry import get_environment_spec, installed_environments
from a2a_engine.storage.schema import apply_schema
from a2a_engine.storage.sqlite import SQLiteEpisodeStore
from a2a_engine.stream_projection import project_events_to_trace
import yaml


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
    version: str | None
    declaration_sha256: str | None
    item_bank_sha256: str | None
    oracle_version: str | None
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
    design_text: str | None = None
    design_sha256: str | None = None
    locked_at: str | None = None
    forked_from: str | None = None


@dataclass(frozen=True)
class Launch:
    id: str
    experiment_id: str
    status: str
    max_parallelism: int
    trace_database: str
    created_at: str
    mode: str = "live"
    execution_path: str | None = None
    shard_index: int | None = None
    shard_count: int | None = None
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class Attempt:
    """One execution of an episode.

    ``episode_id`` is stable across attempts and ``attempt`` is monotonic per
    episode across launches, which is what cleanly attributes every attempt
    back to its experiment and cell.  A failed attempt is kept rather than
    tombstoned: it is diagnostically valuable, and "the result for this
    episode" is a query -- the completed attempt with the highest number -- not
    a stored flag two analyses can disagree about.
    """

    id: str
    launch_id: str
    episode_id: str
    cell_id: str
    episode_idx: int
    attempt: int
    status: str
    episode_uri: str | None = None
    error: str | None = None
    started_at: str | None = None
    ended_at: str | None = None


class OracleUnavailable(ValueError):
    """Raised when a release ships an item bank without an oracle."""

class DesignDigestMismatch(ValueError):
    """A lock or launch received text different from the preregistered digest."""



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
            launch.execution_path or experiment.yaml_path,
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
        if launch.mode == "dry_run":
            command.append("--dry-run")
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
                 results_dir: str | Path | None = None) -> None:
        self.path = Path(path)
        self.workspace = Path(workspace).resolve()
        # One database. The episode store writes ``episodes`` into the same
        # file, which is what lets the fact table join its dimensions in SQL
        # and retires the ``trace_uri`` regex entirely.
        self.trace_database = self.path
        self.results_dir = Path(results_dir) if results_dir else self.path.parent / "results"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.launcher: Launcher = LocalLauncher(self.workspace, self)
        # A launcher persists no PID and its watcher thread dies with the
        # process, so a launch interrupted by a restart would otherwise read
        # RUNNING forever. Nothing is live yet at construction time, so every
        # non-terminal launch found here is by definition stranded.
        self.reconcile()

    def _connect(self) -> sqlite3.Connection:
        # The runner subprocess writes episodes into this same file, so the two
        # owners must agree on journal mode from the first connection: changing
        # it needs an exclusive lock, and a runner that finds the database in
        # rollback mode fails its sink preflight with "database is locked"
        # while the server merely has it open. WAL also lets the viewer read
        # while a launch is writing.
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """One short-lived connection, committed and then actually closed.

        ``with sqlite3.connect(...)`` commits but does not close, which leaked
        a connection per request. That was survivable while this file was the
        control plane's alone; sharing it with a runner subprocess makes every
        lingering handle something the writer has to wait behind.
        """
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _store(self) -> SQLiteEpisodeStore:
        return SQLiteEpisodeStore(path=self.path, results_dir=self.results_dir)

    def _init_db(self) -> None:
        with self._session() as db:
            apply_schema(db)
            db.execute("""
                UPDATE experiments
                SET environment_id = (
                    SELECT releases.environment_id
                    FROM releases
                    WHERE releases.id = experiments.release_id
                )
                WHERE environment_id IS NULL
            """)

    def seed_installed_releases(self) -> list[Release]:
        """Register every installed release as a dimension row.

        The release id is the declaration's own id, not a launcher-local
        invention: the runner stamps that same id into every episode's
        provenance, so the fact table's ``release_id`` resolves here rather
        than into a second namespace nothing can join.
        """
        created: list[Release] = []
        with self._session() as db:
            # Only entry-point games are releases. A environment registered directly at
            # runtime has no package behind it, so offering it as a launchable
            # release would present a release with nothing to run.
            for environment_id in installed_environments():
                spec = get_environment_spec(environment_id)
                declaration = spec.declaration
                if declaration is None:
                    continue
                release = Release(
                    id=str(declaration.id or environment_id),
                    environment_id=environment_id,
                    version=declaration.version,
                    declaration_sha256=declaration.content_sha256(),
                    item_bank_sha256=(
                        declaration.item_policy.item_bank_sha256
                        if declaration.item_policy is not None else None
                    ),
                    oracle_version=declaration.oracle_version,
                    package=spec.package,
                    source_ref="local-workspace",
                    metadata={
                        "launcher": "local",
                        "entrypoint": environment_id,
                        "source_url": declaration.source_url,
                        "blurb": declaration.blurb,
                    },
                    created_at=_now(),
                )
                row = db.execute(
                    "SELECT id FROM releases WHERE id = ?", (release.id,)
                ).fetchone()
                if row:
                    # An episode may have projected this row out of its own
                    # provenance before the control plane ever saw the release;
                    # fill in what only the installed package knows.
                    db.execute(
                        "UPDATE releases SET environment_id = ?, version = ?,"
                        " declaration_sha256 = ?, item_bank_sha256 = ?, oracle_version = ?,"
                        " package = ?, source_ref = ?, metadata = ? WHERE id = ?",
                        (release.environment_id, release.version, release.declaration_sha256,
                         release.item_bank_sha256, release.oracle_version, release.package,
                         release.source_ref, _json(release.metadata), release.id),
                    )
                    continue
                db.execute(
                    "INSERT INTO releases (id, environment_id, version, declaration_sha256,"
                    " item_bank_sha256, oracle_version, package, source_ref, metadata, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (release.id, release.environment_id, release.version,
                     release.declaration_sha256, release.item_bank_sha256,
                     release.oracle_version, release.package, release.source_ref,
                     _json(release.metadata), release.created_at),
                )
                created.append(release)
        return created

    def releases(self) -> list[Release]:
        self.seed_installed_releases()
        with self._session() as db:
            rows = db.execute("SELECT * FROM releases ORDER BY environment_id").fetchall()
        return [self._release(row) for row in rows]

    def environment_summaries(self) -> list[dict[str, Any]]:
        """Return the registered, researcher-visible release catalog."""
        releases = {release.environment_id: release for release in self.releases()}
        with self._session() as db:
            counts = {
                row["environment_id"]: row["experiment_count"]
                for row in db.execute(
                    "SELECT environment_id, COUNT(*) AS experiment_count "
                    "FROM experiments GROUP BY environment_id"
                ).fetchall()
            }
        environments: list[dict[str, Any]] = []
        for environment_id in installed_environments():
            declaration = self._declaration(environment_id)
            release = releases.get(environment_id)
            if release is None:
                continue
            environments.append({
                "environment_id": environment_id,
                "blurb": declaration.blurb,
                "version": declaration.version,
                "release_id": release.id,
                "source_url": declaration.source_url,
                "experiment_count": counts.get(environment_id, 0),
            })
        return environments

    def environment_detail(self, environment_id: str) -> dict[str, Any]:
        declaration = self._declaration(environment_id)
        release = self._release_for(environment_id, None)
        if declaration.item_policy is None:
            raise ValueError(f"environment {environment_id!r} has no item policy")
        bank = self._item_bank(environment_id)
        return {
            "environment_id": environment_id,
            "release": {
                "release_id": release.id,
                "version": declaration.version,
                "declaration_sha256": declaration.content_sha256(),
                "item_bank_sha256": declaration.item_policy.item_bank_sha256,
                "oracle_version": declaration.oracle_version,
            },
            "parameters": [
                self._parameter_view(parameter, bank) for parameter in declaration.parameters
            ],
            "roles": [role.model_dump(mode="json") for role in declaration.roles],
            "measures": [measure.model_dump(mode="json") for measure in declaration.measures],
            "item_policy": {
                **declaration.item_policy.model_dump(mode="json"),
                "item_count": len(bank.items),
            },
            "declaration_yaml": yaml.safe_dump(
                declaration.model_dump(mode="json"), sort_keys=False, allow_unicode=False,
            ),
        }

    @staticmethod
    def _parameter_view(parameter, bank: ItemBank) -> dict[str, Any]:
        """One parameter as the design layer sees it, with item levels projected.

        The declaration itself is left alone — ``declaration_yaml`` and
        ``declaration_sha256`` stay a function of the checked-in source, not of
        whichever bank happens to be on disk.  Derived levels ride alongside.
        """

        view = parameter.model_dump(mode="json")
        if parameter.source != "item":
            view["levels"] = None
            view["identity_grained"] = False
            return view
        domain, levels = derive_item_domain(parameter, bank)
        view["domain"] = list(domain) if isinstance(domain, tuple) else domain
        view["levels"] = [{"value": value, "count": count} for value, count in levels]
        # One distinct value per row means the column is the item's identity
        # restated; there are no strata to choose between.
        view["identity_grained"] = len(levels) == len(bank.items) > 1
        return view

    def environment_items(self, environment_id: str, *, limit: int = 50,
                          cursor: str | None = None) -> dict[str, Any]:
        bank = self._item_bank(environment_id)
        if not 1 <= limit <= 100:
            raise ValueError("item limit must be between 1 and 100")
        items, next_cursor = bank.page(limit=limit, cursor=cursor)
        return {
            "items": [item.summary() for item in items],
            "next_cursor": next_cursor,
            "item_bank_sha256": bank.item_bank_sha256,
        }

    def run_oracle(self, environment_id: str, item_id: str) -> dict[str, Any]:
        declaration = self._declaration(environment_id)
        if declaration.oracle_version is None:
            raise OracleUnavailable(f"environment {environment_id!r} does not ship an oracle")
        item = self._item_bank(environment_id).get(item_id)
        return {
            "item_id": item.item_id,
            "oracle_version": declaration.oracle_version,
            "result": item.oracle_result,
        }

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

    def create_experiment(self, *, yaml_path: str | None = None,
                          release_id: str | None = None, name: str | None = None,
                          design_text: str | None = None,
                          forked_from: str | None = None) -> Experiment:
        """Create either a legacy YAML input or an authored design experiment.

        Direct YAML execution remains available for probing environments. A
        design is the preregistration record instead: its text is retained
        verbatim, while its canonical digest identifies the authored content.
        """
        if design_text is not None:
            if not name:
                raise ValueError("a design experiment needs a name")
            if not release_id:
                raise ValueError("a design experiment needs a release_id")
            design = parse_design_text(design_text)
            release = self._release_by_id(release_id)
            declaration = self._declaration(release.environment_id)
            bank = self._item_bank(release.environment_id)
            errors = validate_design(design, declaration, bank, release_id=release.id)
            if errors:
                raise DesignValidationError(errors)
            experiment = Experiment(
                id=str(uuid.uuid4()), name=name, environment_id=release.environment_id,
                release_id=release.id, yaml_path="", config_sha256=design.content_sha256(),
                created_at=_now(), design_text=design_text,
                design_sha256=design.content_sha256(), forked_from=forked_from,
            )
            with self._session() as db:
                db.execute(
                    "INSERT INTO experiments (id, name, environment_id, release_id, yaml_path, "
                    "config_sha256, design_text, design_sha256, locked_at, forked_from, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (experiment.id, experiment.name, experiment.environment_id,
                     experiment.release_id, experiment.yaml_path, experiment.config_sha256,
                     experiment.design_text, experiment.design_sha256, experiment.locked_at,
                     experiment.forked_from, experiment.created_at),
                )
            return experiment

        if yaml_path is None:
            raise ValueError("provide either design_text or yaml_path")
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
        with self._session() as db:
            db.execute(
                "INSERT INTO experiments (id, name, environment_id, release_id, yaml_path, "
                "config_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (experiment.id, experiment.name, experiment.environment_id,
                 experiment.release_id, experiment.yaml_path, experiment.config_sha256,
                 experiment.created_at),
            )
        return experiment

    def experiments(self) -> list[Experiment]:
        with self._session() as db:
            rows = db.execute("SELECT * FROM experiments ORDER BY created_at DESC").fetchall()
        return [self._experiment(row) for row in rows]

    def experiment_detail(self, experiment_id: str) -> dict[str, Any]:
        experiment = self.experiment(experiment_id)
        with self._session() as db:
            cells = db.execute(
                "SELECT cell_id, levels, episodes_planned FROM cells "
                "WHERE experiment_id = ? ORDER BY cell_id", (experiment_id,),
            ).fetchall()
            participants = db.execute(
                "SELECT participant_id, kind, binding, config_sha256 FROM participants "
                "WHERE experiment_id = ? ORDER BY participant_id", (experiment_id,),
            ).fetchall()
            launches = db.execute(
                "SELECT id FROM launches WHERE experiment_id = ? ORDER BY created_at DESC", (experiment_id,)
            ).fetchall()
        return {
            "experiment": asdict(experiment),
            "cells": [
                {"cell_id": row["cell_id"], "levels": json.loads(row["levels"]),
                 "episodes_planned": row["episodes_planned"]}
                for row in cells
            ],
            "cell_evidence": self._store().cell_evidence(experiment_id),
            "roster": [dict(row) for row in participants],
            "launches": [self.launch_detail(row["id"]) for row in launches],
        }

    def validate_design_text(self, *, release_id: str, design_text: str) -> dict[str, Any]:
        """Validate and preview a draft without writing an experiment record."""
        try:
            design = parse_design_text(design_text)
            release = self._release_by_id(release_id)
            declaration = self._declaration(release.environment_id)
            bank = self._item_bank(release.environment_id)
            errors = validate_design(design, declaration, bank, release_id=release.id)
            if errors:
                return {"valid": False, "errors": [error.as_dict() for error in errors], "plan": None}
            preview = compile_design(
                design, declaration, bank, experiment_id="preview", experiment_name="preview",
                release_id=release.id,
            )
            return {"valid": True, "errors": [], "plan": preview.as_api_dict()}
        except DesignValidationError as exc:
            return {"valid": False, "errors": [error.as_dict() for error in exc.errors], "plan": None}

    def fork_experiment_design(self, experiment_id: str) -> Experiment:
        """Fork a locked preregistration into an editable draft.

        Forking is deliberately its own operation.  A browser must never turn a
        save into a new study behind the researcher's back, especially when the
        page is showing an immutable preregistration.
        """
        experiment = self.experiment(experiment_id)
        if experiment.design_text is None:
            raise ValueError("a legacy YAML experiment has no design document to fork")
        if experiment.locked_at is None:
            raise ValueError("only a locked preregistration can be forked")
        return self.create_experiment(
            name=f"{experiment.name} fork", release_id=experiment.release_id,
            design_text=experiment.design_text, forked_from=experiment.id,
        )

    def update_experiment_design(self, experiment_id: str, *, design_text: str) -> Experiment:
        """Save an editable design draft; locked preregistrations are immutable."""
        experiment = self.experiment(experiment_id)
        if experiment.design_text is None:
            raise ValueError("a legacy YAML experiment has no design document to edit")
        if experiment.locked_at is not None:
            raise ValueError("this preregistration is locked; fork it before editing")
        design = parse_design_text(design_text)
        release = self._release_by_id(experiment.release_id)
        errors = validate_design(
            design, self._declaration(release.environment_id), self._item_bank(release.environment_id),
            release_id=release.id,
        )
        if errors:
            raise DesignValidationError(errors)
        digest = design.content_sha256()
        with self._session() as db:
            db.execute(
                "UPDATE experiments SET design_text = ?, design_sha256 = ?, config_sha256 = ? WHERE id = ?",
                (design_text, digest, digest, experiment.id),
            )
        return self.experiment(experiment_id)

    def lock_experiment(self, experiment_id: str, *, design_sha256: str) -> Experiment:
        """Preregister a design and materialise its entire fixed execution plan."""
        experiment = self.experiment(experiment_id)
        if experiment.design_text is None:
            raise ValueError("only a design experiment can be locked")
        if experiment.locked_at is not None:
            return experiment
        design = parse_design_text(experiment.design_text)
        actual_digest = design.content_sha256()
        if design_sha256 != actual_digest or experiment.design_sha256 != actual_digest:
            raise DesignDigestMismatch("the design text no longer matches its recorded digest; fork it instead")
        release = self._release_by_id(experiment.release_id)
        declaration = self._declaration(release.environment_id)
        bank = self._item_bank(release.environment_id)
        plan = compile_design(
            design, declaration, bank, experiment_id=experiment.id,
            experiment_name=experiment.name, release_id=release.id,
        )
        locked_at = _now()
        with self._session() as db:
            db.execute("DELETE FROM cells WHERE experiment_id = ?", (experiment.id,))
            db.execute("DELETE FROM participants WHERE experiment_id = ?", (experiment.id,))
            self._sync_items(db, bank)
            db.executemany(
                "INSERT INTO cells (cell_id, experiment_id, levels, episodes_planned, episode_configs, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (cell.cell_id, experiment.id, _json(cell.levels), len(cell.episodes),
                     _json([episode.config for episode in cell.episodes]), locked_at)
                    for cell in plan.cells
                ],
            )
            db.executemany(
                "INSERT INTO participants (participant_id, experiment_id, kind, binding, config_sha256) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (participant.id, experiment.id, participant.kind, participant.binding,
                     hashlib.sha256(_json(participant.model_dump(mode="json")).encode("utf-8")).hexdigest())
                    for participant in design.roster
                ],
            )
            db.execute("UPDATE experiments SET locked_at = ? WHERE id = ?", (locked_at, experiment.id))
        return self.experiment(experiment.id)

    def launch_experiment(self, experiment_id: str, *, max_parallelism: int = 1,
                          smoke_test: bool = False, mode: str | None = None,
                          shard_index: int | None = None,
                          shard_count: int | None = None) -> Launch:
        """Launch direct YAML or a fixed design plan.

        A locked design consumes its persisted episode configs.  Smoke and
        dry-run can compile an unlocked draft for feedback, but live execution
        is always gated on its preregistration lock.
        """
        if max_parallelism < 1:
            raise ValueError("max_parallelism must be >= 1")
        mode = "smoke" if smoke_test else (mode or "live")
        if mode not in {"live", "smoke", "dry_run"}:
            raise ValueError("mode must be live, smoke, or dry_run")
        if shard_count is not None and shard_count < 1:
            raise ValueError("shard_count must be >= 1")
        if shard_index is not None and shard_index < 0:
            raise ValueError("shard_index must be >= 0")
        if shard_count is not None and (shard_index or 0) >= shard_count:
            raise ValueError("shard_index must be less than shard_count")
        experiment = self.experiment(experiment_id)
        execution_path: str | None = None
        design_configs: list[dict[str, Any]] | None = None
        if experiment.design_text is not None:
            if mode == "live" and experiment.locked_at is None:
                raise ValueError("live launch requires a locked preregistration")
            design_configs = self._design_episode_configs(experiment, mode=mode)
            effective_shard_count = shard_count or 1
            effective_shard_index = shard_index or 0
            design_configs = [
                config for index, config in enumerate(design_configs)
                if index % effective_shard_count == effective_shard_index
            ]
            if not design_configs:
                raise ValueError("this shard contains no planned episodes")
        else:
            effective_shard_count = None
            effective_shard_index = None

        launch_id = str(uuid.uuid4())
        launch = Launch(
            id=launch_id, experiment_id=experiment.id, status="QUEUED",
            max_parallelism=max_parallelism, trace_database=str(self.trace_database),
            created_at=_now(), mode=mode, shard_index=effective_shard_index,
            shard_count=effective_shard_count,
        )
        attempt_rows: list[tuple[str, str, int, int]]
        with self._session() as db:
            if design_configs is not None:
                attempt_rows = []
                for config in design_configs:
                    episode_id = str(config["episode_id"])
                    cell_id = str(config["provenance"]["cell_id"])
                    episode_idx = int(config["provenance"]["episode_idx"])
                    attempt = self._next_attempt(db, episode_id)
                    config["provenance"] = dict(config["provenance"])
                    config["provenance"]["attempt"] = attempt
                    attempt_rows.append((episode_id, cell_id, episode_idx, attempt))
            else:
                attempt_rows = [
                    (episode_id, cell_id, episode_idx, self._next_attempt(db, episode_id))
                    for episode_id, cell_id, episode_idx in self._planned_attempts(
                        experiment, smoke_test=mode == "smoke"
                    )
                ]

        if design_configs is not None:
            execution_path = self._write_execution_plan(launch.id, experiment, design_configs)
            launch = Launch(
                **{**asdict(launch), "execution_path": execution_path}
            )

        with self._session() as db:
            db.execute(
                "INSERT INTO launches (id, experiment_id, status, max_parallelism, trace_database, "
                "mode, execution_path, shard_index, shard_count, created_at, started_at, ended_at, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (launch.id, launch.experiment_id, launch.status, launch.max_parallelism,
                 launch.trace_database, launch.mode, launch.execution_path, launch.shard_index,
                 launch.shard_count, launch.created_at, launch.started_at, launch.ended_at, launch.error),
            )
            db.executemany(
                "INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (str(uuid.uuid4()), launch.id, episode_id, cell_id, episode_idx,
                     attempt, "QUEUED", None, None, None, None)
                    for episode_id, cell_id, episode_idx, attempt in attempt_rows
                ],
            )
            self._event(db, launch.id, "launch.queued", {
                "episode_count": len(attempt_rows), "mode": mode,
            })
        self.launcher.launch(
            launch, experiment, lambda line: self._line(launch.id, line), smoke_test=mode == "smoke",
        )
        return self.launch(launch.id)

    def _design_episode_configs(self, experiment: Experiment, *, mode: str) -> list[dict[str, Any]]:
        """Read the locked fixed plan or compile an unlocked smoke/dry-run draft."""
        if experiment.design_text is None:
            return []
        design = parse_design_text(experiment.design_text)
        if experiment.locked_at is not None:
            if experiment.design_sha256 != design.content_sha256():
                raise DesignDigestMismatch(
                    "the locked design text changed; fork it instead of launching a different plan"
                )
            with self._session() as db:
                rows = db.execute(
                    "SELECT cell_id, episode_configs FROM cells WHERE experiment_id = ? ORDER BY cell_id",
                    (experiment.id,),
                ).fetchall()
            if not rows:
                raise ValueError("locked experiment has no persisted execution plan")
            configs = [
                config for row in rows for config in json.loads(row["episode_configs"])
            ]
        else:
            release = self._release_by_id(experiment.release_id)
            plan = compile_design(
                design, self._declaration(release.environment_id), self._item_bank(release.environment_id),
                experiment_id=experiment.id, experiment_name=experiment.name, release_id=release.id,
            )
            configs = [episode.config for cell in plan.cells for episode in cell.episodes]

        if mode == "smoke":
            seen_cells: set[str] = set()
            configs = [
                config for config in configs
                if not (config["provenance"]["cell_id"] in seen_cells
                        or seen_cells.add(config["provenance"]["cell_id"]))
            ]
        return [json.loads(_json(config)) for config in configs]

    def _write_execution_plan(
        self, launch_id: str, experiment: Experiment, configs: list[dict[str, Any]]
    ) -> str:
        """Write runner-native YAML from the fixed per-episode design plan."""
        path = self.results_dir / "plans" / experiment.id / f"{launch_id}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        cells = []
        for config in configs:
            provenance = config["provenance"]
            cells.append({
                "label": f"planned-{provenance['cell_id']}-{provenance['episode_idx']:03d}",
                "count": 1,
                "config": {
                    **config,
                    "_design_episode": {
                        "cell_id": provenance["cell_id"],
                        "episode_idx": provenance["episode_idx"],
                        "episode_id": config["episode_id"],
                        "seed": config["seed"],
                        "attempt": provenance["attempt"],
                        "provenance": provenance,
                    },
                },
            })
        path.write_text(
            yaml.safe_dump({"name": experiment.name, "cells": cells}, sort_keys=False),
            encoding="utf-8",
        )
        return str(path)

    def cancel_launch(self, launch_id: str) -> Launch:
        launch = self.launch(launch_id)
        if launch.status not in {"QUEUED", "RUNNING"}:
            return launch
        if self.launcher.cancel(launch_id):
            with self._session() as db:
                db.execute("UPDATE launches SET status = ? WHERE id = ?", ("CANCELLING", launch_id))
                self._event(db, launch_id, "launch.cancelling", {})
        return self.launch(launch_id)

    def launch(self, launch_id: str) -> Launch:
        with self._session() as db:
            row = db.execute("SELECT * FROM launches WHERE id = ?", (launch_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown launch {launch_id}")
        return self._launch(row)

    def launch_detail(self, launch_id: str) -> dict[str, Any]:
        launch = self.launch(launch_id)
        with self._session() as db:
            attempts = db.execute(
                """
                SELECT a.*, (
                    SELECT e.episode_uid
                    FROM episodes e
                    WHERE e.episode_id = a.episode_id AND e.attempt = a.attempt
                    ORDER BY e.created_at DESC
                    LIMIT 1
                ) AS episode_uid
                FROM attempts a
                WHERE a.launch_id = ?
                ORDER BY a.cell_id, a.episode_idx
                """,
                (launch_id,),
            ).fetchall()
        prefix = f"a2a:launch:{launch.id}:episode:"
        return {
            "launch": asdict(launch),
            "attempts": [{
                **asdict(self._attempt(row)),
                # URI provenance remains available to older callers, while the
                # explicit uid is the durable link to a persisted episode.
                "episode_uid": row["episode_uid"],
                "redis_stream": prefix + row["episode_id"],
            } for row in attempts],
            "progress": self.progress(launch_id),
            # This is a durable read of launch_events, not an SSE replay. A
            # terminal launch can therefore be reopened with its runner output.
            "runner_logs": [event for event in self.events(launch_id) if event["kind"] == "runner.log"],
        }

    def planned_episode_ids(self, launch_id: str) -> list[str]:
        """Every episode this launch was planned to execute, written at plan time."""
        with self._session() as db:
            rows = db.execute(
                "SELECT episode_id FROM attempts WHERE launch_id = ? "
                "ORDER BY cell_id, episode_idx",
                (launch_id,),
            ).fetchall()
        return [row["episode_id"] for row in rows]

    def progress(self, launch_id: str) -> dict[str, Any]:
        """Progress as an identity join over deterministic episode ids.

        The whole fan-out is materialised before any episode runs, and the
        episode store answers "which of these ids do you hold" -- the same
        question ``--resume`` asks. Because both tables now live in one
        database that is one query, and because it reads persisted state rather
        than a process's stdout it survives a server restart unchanged.

        A ``PARTIAL`` episode is a recovered fragment, not a completed run, so
        it is deliberately not counted.
        """
        with self._session() as db:
            rows = db.execute(
                """
                SELECT a.cell_id AS cell_id,
                       COUNT(*) AS planned,
                       SUM(CASE WHEN done.episode_id IS NOT NULL THEN 1 ELSE 0 END) AS completed,
                       SUM(CASE WHEN a.status = 'FAILED' THEN 1 ELSE 0 END) AS failed
                FROM attempts a
                LEFT JOIN (
                    SELECT DISTINCT episode_id FROM episodes WHERE status = 'COMPLETED'
                ) done ON done.episode_id = a.episode_id
                WHERE a.launch_id = ?
                GROUP BY a.cell_id
                ORDER BY a.cell_id
                """,
                (launch_id,),
            ).fetchall()
        by_cell = [
            {"cell_id": row["cell_id"], "planned": row["planned"],
             "completed": row["completed"], "failed": row["failed"]}
            for row in rows
        ]
        return {
            "by_cell": by_cell,
            "planned": sum(cell["planned"] for cell in by_cell),
            "completed": sum(cell["completed"] for cell in by_cell),
            "failed": sum(cell["failed"] for cell in by_cell),
        }

    def reconcile(self) -> list[str]:
        """Resolve launches whose runner process did not survive.

        Called at construction, when no launch this instance started can be
        live, so a non-terminal row here means the process that owned it is
        gone. Each attempt is resolved from the identity join; one with no
        episode row is looked for in the durable event log first, because an
        attempt that died mid-episode is recoverable as a partial trace and
        that is evidence worth keeping rather than a gap to write off.
        """
        live = set(getattr(self.launcher, "_processes", {}) or {})
        with self._session() as db:
            stranded = [
                self._launch(row) for row in db.execute(
                    "SELECT * FROM launches WHERE status IN ('QUEUED', 'RUNNING', 'CANCELLING')"
                ).fetchall()
            ]
        resolved: list[str] = []
        for launch in stranded:
            if launch.id in live:
                continue
            self._reconcile_launch(launch)
            resolved.append(launch.id)
        return resolved

    def _reconcile_launch(self, launch: Launch) -> None:
        cancelled = launch.status == "CANCELLING"
        now = _now()
        with self._session() as db:
            attempts = [
                self._attempt(row) for row in db.execute(
                    "SELECT * FROM attempts WHERE launch_id = ? AND status IN ('QUEUED', 'RUNNING')",
                    (launch.id,),
                ).fetchall()
            ]
            completed = {
                (row["episode_id"], row["attempt"]): row["episode_uid"]
                for row in db.execute(
                    "SELECT episode_id, attempt, episode_uid FROM episodes WHERE status = 'COMPLETED'"
                ).fetchall()
            }
        recovered: list[str] = []
        for attempt in attempts:
            episode_uid = completed.get((attempt.episode_id, attempt.attempt))
            if episode_uid is not None:
                self._resolve_attempt(
                    attempt, status="COMPLETED",
                    episode_uri=self._store().uri(episode_uid), error=None, ended_at=now,
                )
                continue
            partial_uri = self._recover_partial(attempt)
            if partial_uri is not None:
                recovered.append(attempt.episode_id)
            self._resolve_attempt(
                attempt,
                status="CANCELLED" if cancelled else "UNREPORTED",
                episode_uri=partial_uri,
                error=(
                    "the runner process did not survive; a partial trace was recovered "
                    "from this episode's event log"
                    if partial_uri else
                    "the runner process did not survive and reported no result for this episode"
                ),
                ended_at=now,
            )
        with self._session() as db:
            outstanding = db.execute(
                "SELECT COUNT(*) AS n FROM attempts WHERE launch_id = ? AND status != 'COMPLETED'",
                (launch.id,),
            ).fetchone()["n"]
            status = "CANCELLED" if cancelled else ("COMPLETED" if not outstanding else "FAILED")
            db.execute(
                "UPDATE launches SET status = ?, ended_at = ?, error = ? WHERE id = ?",
                (status, now,
                 None if status == "COMPLETED" else "reconciled after restart: the runner "
                 "process did not survive",
                 launch.id),
            )
            self._event(db, launch.id, "launch.reconciled", {
                "status": status, "recovered_episode_ids": recovered,
            })
            self._event(db, launch.id, f"launch.{status.lower()}", {"reconciled": True})

    def _resolve_attempt(self, attempt: Attempt, *, status: str, episode_uri: str | None,
                         error: str | None, ended_at: str) -> None:
        with self._session() as db:
            db.execute(
                "UPDATE attempts SET status = ?, episode_uri = ?, error = ?, ended_at = ? "
                "WHERE id = ?",
                (status, episode_uri, error, ended_at, attempt.id),
            )

    def _recover_partial(self, attempt: Attempt) -> str | None:
        """Persist whatever the interrupted episode's event log holds.

        The sink was flushed event by event as the episode played, so a run
        killed between two events still has everything up to that point. The
        recovered trace is stored with ``status = PARTIAL``: it is evidence for
        inspection and retry, never a successful experimental result.
        """
        for path in iter_event_sinks(self.results_dir):
            entries = read_event_sink(path)
            if not entries or entries[0].get("episode_id") != attempt.episode_id:
                continue
            try:
                trace = project_events_to_trace(path)
            except ValueError:
                continue
            experiment_name = path.parent.name
            manifest = EpisodeManifest.from_run(
                config=trace.config.model_dump(),
                experiment_name=experiment_name,
                cell_id=attempt.cell_id,
                episode_idx=attempt.episode_idx,
                episode_uid=trace.episode_uid,
            )
            manifest.episode_id = attempt.episode_id
            manifest.environment_id = str(trace.config.environment_id or "")
            return self._store().put_episode(trace, manifest)
        return None

    @staticmethod
    def _next_attempt(db: sqlite3.Connection, episode_id: str) -> int:
        row = db.execute(
            "SELECT COALESCE(MAX(attempt), 0) + 1 AS next FROM attempts WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        return int(row["next"])

    def launches(self) -> list[Launch]:
        with self._session() as db:
            rows = db.execute("SELECT * FROM launches ORDER BY created_at DESC").fetchall()
        return [self._launch(row) for row in rows]

    def events(self, launch_id: str, *, after_id: int = 0) -> list[dict[str, Any]]:
        with self._session() as db:
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
        with self._session() as db:
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

    def _declaration(self, environment_id: str):
        spec = get_environment_spec(environment_id)
        declaration = spec.declaration
        if declaration is None:
            raise KeyError(f"environment {environment_id!r} does not publish a release declaration")
        if declaration.environment_id != environment_id:
            raise ValueError(
                f"environment {environment_id!r} registered a declaration for "
                f"{declaration.environment_id!r}"
            )
        return declaration

    def _item_bank(self, environment_id: str) -> ItemBank:
        declaration = self._declaration(environment_id)
        if declaration.item_policy is None:
            raise ValueError(f"environment {environment_id!r} has no item policy")
        return ItemBank.load(
            self._workspace_path(declaration.item_policy.bank_path),
            expected_sha256=declaration.item_policy.item_bank_sha256,
        )

    @staticmethod
    def _sync_items(db: sqlite3.Connection, bank: ItemBank) -> None:
        """Project the frozen bank rows selected by a locked design into SQL."""
        db.executemany(
            "INSERT INTO items (item_id, item_bank_sha256, params, oracle_result) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET item_bank_sha256 = excluded.item_bank_sha256, "
            "params = excluded.params, oracle_result = excluded.oracle_result",
            [
                (item.item_id, bank.item_bank_sha256, _json(item.params), _json(item.oracle_result))
                for item in bank.items
            ],
        )

    def _release_by_id(self, release_id: str) -> Release:
        self.seed_installed_releases()
        with self._session() as db:
            row = db.execute("SELECT * FROM releases WHERE id = ?", (release_id,)).fetchone()
        if row is None:
            raise ValueError(f"no release registered as {release_id!r}")
        return self._release(row)

    def _release_for(self, environment_id: str, release_id: str | None) -> Release:
        self.seed_installed_releases()
        with self._session() as db:
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
                # Only the latest release is launchable while per-release
                # runtime images are deferred; the release is still recorded on
                # every episode.
                row = db.execute(
                    "SELECT * FROM releases WHERE environment_id = ? "
                    "ORDER BY created_at DESC, id LIMIT 1",
                    (environment_id,),
                ).fetchone()
        if row is None:
            raise ValueError(f"no local release registered for environment {environment_id!r}")
        return self._release(row)

    def _line(self, launch_id: str, line: str) -> None:
        """Fold one runner stdout line into the launch event log.

        This is a *latency* path, not a truth path: it is what makes a live
        launch feel responsive before the next progress query. Whether an
        episode actually exists is settled by the identity join in
        :meth:`progress` and :meth:`_finish_launch`, which is why a lost line
        can no longer strand an attempt that in fact completed.
        """
        if not line:
            return
        with self._session() as db:
            self._event(db, launch_id, "runner.log", {"line": line})
            result = _LIVE_RESULT.search(line) or _SMOKE_RESULT.search(line)
            if result:
                identifier, uri = result["episode"], result["uri"]
                # The URI is useful provenance, but an emitted line is never
                # proof that a trace was committed. _finish_launch settles the
                # attempt from the episodes identity join.
                db.execute(
                    "UPDATE attempts SET episode_uri = COALESCE(episode_uri, ?) "
                    "WHERE launch_id = ? AND episode_id = ?",
                    (uri, launch_id, identifier),
                )
                self._event(db, launch_id, "episode.reported", {"episode_id": identifier, "episode_uri": uri})
                return
            # A failed episode reports its own diagnostic. Attributing it to
            # that attempt keeps the per-episode error out of the launch-wide
            # exit status, which cannot say which run broke.
            failure = _LIVE_FAILURE.search(line) or _SMOKE_FAILURE.search(line)
            if failure:
                identifier, error = failure["episode"], failure["error"].strip()
                db.execute(
                    "UPDATE attempts SET error = ? WHERE launch_id = ? AND episode_id = ?",
                    (error, launch_id, identifier),
                )
                self._event(db, launch_id, "episode.failed", {"episode_id": identifier, "error": error})

    def _mark_launch_started(self, launch_id: str) -> None:
        now = _now()
        with self._session() as db:
            db.execute("UPDATE launches SET status = ?, started_at = ? WHERE id = ?", ("RUNNING", now, launch_id))
            db.execute("UPDATE attempts SET status = ?, started_at = ? WHERE launch_id = ? AND status = ?", ("RUNNING", now, launch_id, "QUEUED"))
            self._event(db, launch_id, "launch.started", {})

    def _finish_launch(self, launch_id: str, code: int) -> None:
        with self._session() as db:
            current = db.execute("SELECT status, mode FROM launches WHERE id = ?", (launch_id,)).fetchone()
            cancelled = current and current["status"] == "CANCELLING"
            status = "CANCELLED" if cancelled else ("COMPLETED" if code == 0 else "FAILED")
            error = None if code == 0 or cancelled else f"runner exited with status {code}"
            now = _now()
            db.execute("UPDATE launches SET status = ?, ended_at = ?, error = ? WHERE id = ?", (status, now, error, launch_id))
            # Settle every still-open attempt against the episodes table before
            # judging it silent. A dropped stdout line is a reporting failure,
            # not a missing episode, and the join can tell the two apart.
            db.execute(
                """
                UPDATE attempts
                SET status = 'COMPLETED', ended_at = ?,
                    episode_uri = COALESCE(episode_uri, (
                        SELECT ? || e.episode_uid FROM episodes e
                        WHERE e.episode_id = attempts.episode_id
                          AND e.attempt = attempts.attempt
                          AND e.status = 'COMPLETED'
                        LIMIT 1
                    ))
                WHERE launch_id = ? AND episode_id IN (
                    SELECT e.episode_id FROM episodes e
                    WHERE e.attempt = attempts.attempt AND e.status = 'COMPLETED'
                )
                """,
                (now, f"{self._store().uri()}#", launch_id),
            )
            if current and current["mode"] == "dry_run" and code == 0 and not cancelled:
                db.execute(
                    "UPDATE attempts SET status = ?, ended_at = ?, error = NULL "
                    "WHERE launch_id = ? AND status != 'COMPLETED'",
                    ("DRY_RUN", now, launch_id),
                )
            elif code == 0 and not cancelled:
                # The runner exits zero only after every scheduled run
                # succeeded, so a still-RUNNING attempt means the CLI never
                # reported that episode. Recording it as COMPLETED would
                # invent a result and a missing trace URI; UNREPORTED keeps
                # the gap visible instead.
                unreported = [
                    row["episode_id"]
                    for row in db.execute(
                        "SELECT episode_id FROM attempts WHERE launch_id = ? AND status != ?",
                        (launch_id, "COMPLETED"),
                    ).fetchall()
                ]
                if unreported:
                    db.execute(
                        "UPDATE attempts SET status = ?, ended_at = ?, error = ? "
                        "WHERE launch_id = ? AND status != ?",
                        ("UNREPORTED", now, "runner exited 0 without reporting this episode",
                         launch_id, "COMPLETED"),
                    )
                    self._event(db, launch_id, "launch.unreported_episodes",
                                {"episode_ids": unreported})
            else:
                db.execute(
                    "UPDATE attempts SET status = ?, ended_at = ?, error = COALESCE(error, ?) "
                    "WHERE launch_id = ? AND status != ?",
                    ("CANCELLED" if cancelled else "FAILED", now, error, launch_id, "COMPLETED"),
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
        return Release(
            row["id"], row["environment_id"], row["version"], row["declaration_sha256"],
            row["item_bank_sha256"], row["oracle_version"], row["package"],
            row["source_ref"], json.loads(row["metadata"] or "{}"), row["created_at"],
        )

    @staticmethod
    def _experiment(row: sqlite3.Row) -> Experiment:
        return Experiment(**dict(row))

    @staticmethod
    def _launch(row: sqlite3.Row) -> Launch:
        return Launch(**dict(row))

    @staticmethod
    def _attempt(row: sqlite3.Row) -> Attempt:
        values = dict(row)
        return Attempt(**{field: values[field] for field in Attempt.__dataclass_fields__})
