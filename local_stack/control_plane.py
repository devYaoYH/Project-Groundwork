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

Launching is a *dispatch*, not a spawn.  The control plane publishes the plan
as a digest-bound artifact, hands a launcher the reference, persists the opaque
handle it gets back, and stops caring how the work runs.  Every status
transition afterwards belongs to :class:`local_stack.reconciler.Reconciler`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from a2a_engine.artifacts import (
    ArtifactRef, fetch_verified, make_artifact_store, plan_key, sha256_bytes,
)
from a2a_engine.compiler import ExecutionPlan as CompiledExecutionPlan
from a2a_engine.compiler import compile as compile_design
from a2a_engine.compiler import validate as validate_design
from a2a_engine.design import DesignValidationError, ValidationIssue, parse_design_text
from a2a_engine.event_sink import read_event_sink
from a2a_engine.experiment import expand_cells, load_experiment
from a2a_engine.items import ItemBank, derive_item_domain
from a2a_engine.manifest import EpisodeManifest
from a2a_engine.release_surface import PublishedRelease
from a2a_engine.registry import get_environment_spec, installed_environments
from a2a_engine.schemas import EpisodeTrace
from a2a_engine.storage import ControlPlaneReader, make_control_plane_reader
from a2a_engine.storage.results import (
    latest_result_attempts, latest_result_traces,
    summarize_cell_evidence,
)
from a2a_engine.storage.schema import apply_schema
from a2a_engine.stream_projection import project_events_to_trace
import yaml

from local_stack.launchers import (
    SMOKE_EPISODES_PER_CELL,
    ExecutionHandle,
    ExecutionStatus,
    LaunchInputRef,
    Launcher,
    make_launcher,
)
from local_stack.reconciler import Reconciler

log = logging.getLogger("local_stack.control_plane")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _attempt_diagnostic(execution: ExecutionStatus, *, cancelled: bool, partial: bool) -> str:
    """Say why an attempt left no completed episode, without claiming a cause.

    The evidence is identical in every branch -- no ``COMPLETED`` row -- so
    this describes the execution around it rather than the result, and says so
    when a partial trace was recovered instead of written off.
    """
    if cancelled:
        subject = "this execution was cancelled"
    elif execution is ExecutionStatus.FAILED:
        subject = "the worker execution failed"
    elif execution is ExecutionStatus.SUCCEEDED:
        subject = "the worker execution finished"
    else:
        subject = "the worker execution did not survive"
    if partial:
        return (
            f"{subject}; a partial trace was recovered from this episode's event log"
        )
    return f"{subject} and persisted no episode for this attempt"


def _launch_diagnostic(execution: ExecutionStatus) -> str:
    if execution is ExecutionStatus.FAILED:
        return "the worker execution failed; settled from persisted evidence"
    if execution is ExecutionStatus.SUCCEEDED:
        return "the worker execution finished without persisting every planned episode"
    if execution is ExecutionStatus.CANCELLED:
        return "the execution was cancelled; settled from persisted evidence"
    return "the worker execution did not survive; settled from persisted evidence"


def _roleless_design_sha256(design) -> str:
    """Reproduce the canonical digest from before ``ParticipantConfig.role``.

    Adding an optional Pydantic field makes current ``model_dump`` output
    include ``role: null``. Historic role-less designs were hashed before that
    field existed, so their otherwise valid digest intentionally omits the
    key. This is used only after the source text proves every roster entry
    truly omits the field.
    """
    payload = design.model_dump(mode="json")
    for participant in payload["roster"]:
        participant.pop("role", None)
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


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
    image_digest: str | None = None
    manifest: dict[str, Any] | None = None


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
    pinned_image_digest: str | None = None
    forked_from: str | None = None
    authored_design_text: str | None = None
    authored_design_sha256: str | None = None
    authored_config_sha256: str | None = None
    role_normalization: dict[str, Any] | None = None


@dataclass(frozen=True)
class Launch:
    id: str
    experiment_id: str
    status: str
    max_parallelism: int
    trace_database: str
    created_at: str
    mode: str = "live"
    provenance_grade: str = "unverified"
    image_digest: str | None = None
    execution_path: str | None = None
    # The launch input as the worker sees it: an address and the digest it must
    # hash to. Nothing else crosses -- no workspace path, no experiment id.
    launch_input_uri: str | None = None
    launch_input_sha256: str | None = None
    # Opaque to everything here except the launcher that minted it.
    execution_handle: str | None = None
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


class ControlPlane:
    """Persistence, validation, and event log for local releases and launches."""

    def __init__(self, path: str | Path, *, workspace: str | Path,
                 results_dir: str | Path | None = None,
                 artifact_root: str | Path | None = None,
                 launcher_spec: dict[str, Any] | None = None,
                 store_spec: dict[str, Any] | None = None,
                 colocated_reads: bool | None = None) -> None:
        self.path = Path(path)
        self.workspace = Path(workspace).resolve()
        # One database. The episode store writes ``episodes`` into the same
        # file, which is what lets the fact table join its dimensions in SQL
        # and retires the ``trace_uri`` regex entirely.
        self.trace_database = self.path
        self.results_dir = Path(results_dir) if results_dir else self.path.parent / "results"
        # Where launch inputs are published. Separate from ``results_dir``
        # already, because Phase 2 gives the worker a results directory the
        # control plane cannot read while the artifact root stays shared.
        self.artifact_root = (
            Path(artifact_root) if artifact_root else self.results_dir / "artifacts"
        )
        # Resolved by name, not imported. Locally it is the same file the
        # control-plane tables live in, which is what keeps the single-statement
        # join available as an optimization.
        self.store_spec = dict(store_spec or {"backend": "sqlite", "path": str(self.path)})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.artifacts = make_artifact_store(
            {"backend": "local"}, root=self.artifact_root
        )
        self.launcher: Launcher = make_launcher({
            "backend": "local_process",
            "workspace": self.workspace,
            # The worker publishes here; it gets a results directory of its own.
            "artifact_root": self.artifact_root,
            **(launcher_spec or {}),
        })
        # Whether the episode store is the same file the control-plane tables
        # live in. When it is, launch truth can be settled in one statement;
        # when it is not, the two-step is the only implementation that works,
        # and both must agree.
        self.colocated_reads = (
            colocated_reads if colocated_reads is not None
            else Path(getattr(self._store(), "path", "")) == self.path
        )
        self.reconciler = Reconciler(self)
        # Launches this process is between committing and submitting. A
        # handle-less launch is normally stranded, but for the width of one
        # submit() it is merely young, and the server is threaded: a read
        # arriving in that window must not settle a launch that is starting.
        # Deliberately in-memory and deliberately not persisted -- after a
        # restart the set is empty, which is exactly when a handle-less launch
        # really is an orphan nobody is dispatching.
        self._dispatching: set[str] = set()
        self._dispatching_lock = threading.Lock()
        # A launcher this instance did not start owns nothing, so every
        # non-terminal launch found at construction is by definition stranded
        # and settles from the evidence its worker managed to persist.
        self.reconciler.tick()

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

    def _store(self) -> ControlPlaneReader:
        """The episode store, resolved by name rather than imported.

        Locally this still lands on the same SQLite file, so nothing about the
        one-command workflow changes -- but the control plane no longer *names*
        a backend, which is what has to be true before the episodes it reads
        can live anywhere else.
        """
        return make_control_plane_reader(self.store_spec, results_dir=self.results_dir)

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
            self._backfill_compatible_word_guess_roles(db)

    @staticmethod
    def _backfill_compatible_word_guess_roles(db: sqlite3.Connection) -> None:
        """Normalize only fully compatible legacy Word Guess design metadata.

        A locked design is normally immutable, so this exception retains the
        original authored text and its digests in an additive audit record.
        The current design becomes a role-complete metadata revision, while
        frozen episode configs and already-written traces keep the prior
        provenance digest explicitly recorded by that audit record.
        """
        rows = db.execute(
            "SELECT e.id AS experiment_id, e.design_text, e.design_sha256, e.config_sha256, "
            "e.role_normalization, p.participant_id, p.kind, p.binding, p.role "
            "FROM participants AS p "
            "JOIN experiments AS e ON e.id = p.experiment_id "
            "WHERE e.environment_id = 'word_guess' AND e.locked_at IS NOT NULL "
            "ORDER BY p.experiment_id, p.participant_id"
        ).fetchall()
        by_experiment: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_experiment.setdefault(str(row["experiment_id"]), []).append(row)

        for experiment_id, roster in by_experiment.items():
            experiment = roster[0]
            if (
                len(roster) != 2
                or experiment["role_normalization"] is not None
                or not isinstance(experiment["design_text"], str)
                or not isinstance(experiment["design_sha256"], str)
                or experiment["config_sha256"] != experiment["design_sha256"]
            ):
                continue
            roles: dict[str, str] = {}
            for row in roster:
                participant_id = str(row["participant_id"])
                match = re.fullmatch(r"(guesser|host)_([1-9][0-9]*)", participant_id)
                if match is None or str(row["kind"]) not in {"llm", "scripted", "human"}:
                    roles = {}
                    break
                if row["kind"] != "human" and not row["binding"]:
                    roles = {}
                    break
                roles[participant_id] = match.group(1)
            if set(roles.values()) != {"guesser", "host"}:
                continue
            persisted_roles = {
                str(row["participant_id"]): row["role"] for row in roster
            }
            # The first Phase 2 rollout could have supplied the lane metadata
            # without rewriting its role-less authored document. Treat that
            # exact, complete prior backfill as compatible, but reject a
            # partially populated or conflicting roster rather than guessing
            # what an operator intended.
            if any(
                role is not None and role != roles[participant_id]
                for participant_id, role in persisted_roles.items()
            ) or (any(role is None for role in persisted_roles.values())
                  and any(role is not None for role in persisted_roles.values())):
                continue
            try:
                authored = parse_design_text(experiment["design_text"])
                raw_design = yaml.safe_load(experiment["design_text"])
            except DesignValidationError:
                continue
            raw_roster = raw_design.get("roster") if isinstance(raw_design, dict) else None
            source_omits_roles = isinstance(raw_roster, list) and all(
                isinstance(participant, dict) and "role" not in participant
                for participant in raw_roster
            )
            current_digest = authored.content_sha256()
            legacy_digest = _roleless_design_sha256(authored) if source_omits_roles else None
            if experiment["design_sha256"] not in {current_digest, legacy_digest}:
                continue
            prior_digest = str(experiment["design_sha256"])
            by_id = {participant.id: participant for participant in authored.roster}
            if len(by_id) != len(authored.roster) or set(by_id) != set(roles):
                continue
            if authored.release not in {"word_guess", "word_guess@v1"}:
                continue
            if any(
                participant.role is not None
                or participant.kind != row["kind"]
                or participant.binding != row["binding"]
                for row in roster
                for participant in [by_id[str(row["participant_id"])]]
            ):
                continue
            plans = db.execute(
                "SELECT episode_configs FROM cells WHERE experiment_id = ?", (experiment_id,)
            ).fetchall()
            try:
                configs = [
                    config for cell in plans for config in json.loads(cell["episode_configs"])
                ]
            except (TypeError, json.JSONDecodeError):
                continue
            if not configs or any(
                not isinstance(config, dict)
                or not isinstance(config.get("provenance"), dict)
                or config["provenance"].get("design_sha256") != prior_digest
                for config in configs
            ):
                continue
            normalized_payload = authored.model_dump(mode="json")
            for participant in normalized_payload["roster"]:
                participant["role"] = roles[str(participant["id"])]
            normalized_text = yaml.safe_dump(normalized_payload, sort_keys=False, allow_unicode=False)
            normalized = parse_design_text(normalized_text)
            normalized_digest = normalized.content_sha256()
            audit = {
                "version": "word_guess_role_normalization_v1",
                "normalized_at": _now(),
                "roles": roles,
                "prior": {
                    "design_sha256": prior_digest,
                    "config_sha256": experiment["config_sha256"],
                    "execution_plan_design_sha256": prior_digest,
                },
                "normalized": {
                    "design_sha256": normalized_digest,
                    "config_sha256": normalized_digest,
                },
            }
            db.execute(
                "UPDATE experiments SET design_text = ?, design_sha256 = ?, config_sha256 = ?, "
                "authored_design_text = ?, authored_design_sha256 = ?, "
                "authored_config_sha256 = ?, role_normalization = ? WHERE id = ?",
                (
                    normalized_text, normalized_digest, normalized_digest,
                    experiment["design_text"], prior_digest, experiment["config_sha256"],
                    _json(audit), experiment_id,
                ),
            )
            db.executemany(
                "UPDATE participants SET role = ? "
                "WHERE experiment_id = ? AND participant_id = ? AND role IS NULL",
                [(role, experiment_id, participant_id) for participant_id, role in roles.items()],
            )

    def seed_installed_releases(self) -> list[Release]:
        """Register every installed release as a dimension row.

        The release id is the declaration's own id, not a launcher-local
        invention: the runner stamps that same id into every episode's
        provenance, so the fact table's ``release_id`` resolves here rather
        than into a second namespace nothing can join.
        """
        published = set()
        for path in sorted(self.workspace.glob("games/*/runtime/release.manifest.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            with self._session() as db:
                current = db.execute(
                    "SELECT environment_id, manifest FROM releases WHERE id = ?",
                    (payload.get("release_id"),),
                ).fetchone()
                locked = db.execute(
                    "SELECT 1 FROM experiments WHERE release_id = ? AND locked_at IS NOT NULL LIMIT 1",
                    (payload.get("release_id"),),
                ).fetchone()
            if current and (current["manifest"] or locked):
                # Discovery is additive, never a hidden release update. A rebuilt
                # manifest needs an explicit ingest (and a new ID if locked).
                published.add(current["environment_id"])
                continue
            release = self.ingest_release(payload)
            published.add(release.environment_id)
        created: list[Release] = []
        with self._session() as db:
            # Only entry-point games are releases. A environment registered directly at
            # runtime has no package behind it, so offering it as a launchable
            # release would present a release with nothing to run.
            for environment_id in installed_environments():
                if environment_id in published:
                    continue
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
                    "SELECT id, manifest FROM releases WHERE id = ?", (release.id,)
                ).fetchone()
                if row:
                    if row["manifest"]:
                        continue
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

    def ingest_release(self, payload: dict[str, Any]) -> Release:
        """Validate a published design surface before making it selectable."""
        manifest = PublishedRelease.model_validate(payload)
        release = Release(
            id=manifest.release_id, environment_id=manifest.environment_id,
            version=manifest.release, declaration_sha256=manifest.declaration_sha256,
            item_bank_sha256=(manifest.surface.item_policy.item_bank_sha256
                              if manifest.surface.item_policy else None),
            oracle_version=manifest.surface.oracle_version, package=manifest.package,
            source_ref="published-manifest",
            metadata={"entrypoint": manifest.environment_id,
                      "source_url": manifest.surface.source_url,
                      "blurb": manifest.surface.blurb},
            created_at=_now(), image_digest=manifest.image_digest,
            manifest=manifest.model_dump(mode="json"),
        )
        with self._session() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT image_digest, declaration_sha256, item_bank_sha256, manifest "
                "FROM releases WHERE id = ?", (release.id,),
            ).fetchone()
            locked = db.execute(
                "SELECT 1 FROM experiments WHERE release_id = ? AND locked_at IS NOT NULL LIMIT 1",
                (release.id,),
            ).fetchone()
            if existing and locked and (
                existing["image_digest"] != release.image_digest
                or existing["declaration_sha256"] != release.declaration_sha256
                or existing["item_bank_sha256"] != release.item_bank_sha256
                or (existing["manifest"] is not None and
                    json.loads(existing["manifest"]) != release.manifest)
            ):
                raise ValueError(
                    f"release {release.id!r} is used by a locked experiment; "
                    "publish a new release ID instead of replacing its image or design inputs"
                )
            db.execute(
                "INSERT INTO releases (id, environment_id, version, declaration_sha256, "
                "item_bank_sha256, oracle_version, package, source_ref, metadata, created_at, "
                "image_digest, manifest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET environment_id=excluded.environment_id, "
                "version=excluded.version, declaration_sha256=excluded.declaration_sha256, "
                "item_bank_sha256=excluded.item_bank_sha256, oracle_version=excluded.oracle_version, "
                "package=excluded.package, source_ref=excluded.source_ref, metadata=excluded.metadata, "
                "image_digest=excluded.image_digest, manifest=excluded.manifest",
                (release.id, release.environment_id, release.version, release.declaration_sha256,
                 release.item_bank_sha256, release.oracle_version, release.package, release.source_ref,
                 _json(release.metadata), release.created_at, release.image_digest, _json(release.manifest)),
            )
        return release

    def _design_declaration(self, release: Release):
        if release.manifest:
            return PublishedRelease.model_validate(release.manifest).design_declaration()
        return self._declaration(release.environment_id)

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
        for environment_id, release in sorted(releases.items()):
            declaration = self._design_declaration(release)
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
        release = self._release_for(environment_id, None)
        declaration = self._design_declaration(release)
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
        declaration = self._design_declaration(self._release_for(environment_id, None))
        if declaration.oracle_version is None:
            raise OracleUnavailable(f"environment {environment_id!r} does not ship an oracle")
        item = self._item_bank(environment_id).get(item_id)
        if item.oracle_result is None:
            raise OracleUnavailable(f"item {item_id!r} has no published oracle result")
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
        presence = self.credential_presence(pool.required_credentials(sorted(pool.agents)))
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
                "credential_present": presence.get(entry.credential, False) if entry.credential else None,
            })
        return {"agents": entries, "sources": pool.sources,
                "credential_presence": presence}

    def credential_presence(self, names: list[str]) -> dict[str, bool]:
        probe = getattr(self.launcher, "credential_presence", None)
        return probe(names) if probe else {name: False for name in names}

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
        from a2a_engine.llm.factory import detect_provider, env_var_for_provider

        path = self._workspace_path(yaml_path)
        spec = load_experiment(path)
        from a2a_engine.agent_pool import load_agent_pool
        pool = load_agent_pool(path)
        presence = self.credential_presence(pool.required_credentials(sorted(pool.agents)))

        agents: list[dict[str, Any]] = []
        seen: set[str] = set()
        declares_agents = False
        for cell, resolved in expand_cells(spec):
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
                    "credential_present": presence.get(env_var, False) if env_var else None,
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
            declaration = self._design_declaration(release)
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
                "SELECT participant_id, kind, binding, role, config_sha256 FROM participants "
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
            "cell_evidence": self.cell_evidence(experiment_id),
            "roster": [dict(row) for row in participants],
            "launches": [self.launch_detail(row["id"]) for row in launches],
        }

    def cell_evidence(self, experiment_id: str) -> list[dict[str, Any]]:
        if self.colocated_reads:
            return self._store().cell_evidence(experiment_id)
        return self._cell_evidence_two_step(experiment_id)

    def _cell_evidence_two_step(self, experiment_id: str) -> list[dict[str, Any]]:
        with self._session() as db:
            cells = [dict(row) for row in db.execute(
                "SELECT cell_id, levels, episodes_planned, episode_configs "
                "FROM cells WHERE experiment_id = ? ORDER BY cell_id", (experiment_id,),
            )]
            attempts = [dict(row) for row in db.execute(
                "SELECT a.id, a.cell_id, a.episode_id, a.attempt, a.status, "
                "l.mode AS run_mode, l.provenance_grade FROM attempts a JOIN launches l ON l.id = a.launch_id "
                "JOIN cells c ON c.cell_id = a.cell_id WHERE c.experiment_id = ?",
                (experiment_id,),
            )]
        latest = latest_result_attempts(attempts)
        traces = latest_result_traces(self._store().execution_records(list(latest)))
        executions = []
        for episode_id, attempt in latest.items():
            trace = traces.get((episode_id, int(attempt["attempt"])))
            executions.append({
                **attempt,
                "trace_status": trace["status"] if trace else None,
                "metrics": trace["metrics"] if trace else None,
            })
        return summarize_cell_evidence(cells, executions)

    def validate_design_text(self, *, release_id: str, design_text: str) -> dict[str, Any]:
        """Validate and preview a draft without writing an experiment record."""
        try:
            design = parse_design_text(design_text)
            release = self._release_by_id(release_id)
            declaration = self._design_declaration(release)
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

    def participant_roster(self, experiment_id: str) -> list[dict[str, Any]]:
        """The reviewed roster metadata used only for read-time trace projections."""
        with self._session() as db:
            rows = db.execute(
                "SELECT participant_id, kind, binding, role FROM participants "
                "WHERE experiment_id = ? ORDER BY participant_id",
                (experiment_id,),
            ).fetchall()
        return [dict(row) for row in rows]

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
            design, self._design_declaration(release), self._item_bank(release.environment_id),
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
        declaration = self._design_declaration(release)
        bank = self._item_bank(release.environment_id)
        plan = compile_design(
            design, declaration, bank, experiment_id=experiment.id,
            experiment_name=experiment.name, release_id=release.id,
        )
        locked_at = _now()
        with self._session() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT image_digest, declaration_sha256, item_bank_sha256 FROM releases WHERE id = ?",
                (release.id,),
            ).fetchone()
            if current is None or (current["image_digest"], current["declaration_sha256"],
                                   current["item_bank_sha256"]) != (
                                       release.image_digest, release.declaration_sha256,
                                       release.item_bank_sha256):
                raise ValueError("release changed while locking; validate against the current release")
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
                "INSERT INTO participants (participant_id, experiment_id, kind, binding, role, config_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (participant.id, experiment.id, participant.kind, participant.binding, participant.role,
                     hashlib.sha256(_json(participant.model_dump(mode="json")).encode("utf-8")).hexdigest())
                    for participant in design.roster
                ],
            )
            db.execute(
                "UPDATE experiments SET locked_at = ?, pinned_image_digest = ? WHERE id = ?",
                (locked_at, release.image_digest or "", experiment.id),
            )
        return self.experiment(experiment.id)

    def launch_experiment(self, experiment_id: str, *, max_parallelism: int = 1,
                          smoke_test: bool = False, mode: str | None = None,
                          shard_index: int | None = None,
                          shard_count: int | None = None, force: bool = False) -> Launch:
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
        if shard_index is not None:
            raise ValueError("shard_index belongs to a worker, not a launch")
        if not isinstance(force, bool):
            raise ValueError("force must be a boolean")
        experiment = self.experiment(experiment_id)
        execution_path: str | None = None
        design_configs: list[dict[str, Any]] | None = None
        design_bank: ItemBank | None = None
        if experiment.design_text is not None:
            if mode == "live" and experiment.locked_at is None:
                raise ValueError("live launch requires a locked preregistration")
            # Smoke launches may run an unlocked draft, so locking has not yet
            # projected its frozen item bank into the shared trace database.
            # Every compiled episode references one of these item ids.
            design_bank = self._item_bank(experiment.environment_id)
            design_configs = self._design_episode_configs(experiment, mode=mode)
            if not design_configs:
                raise ValueError("this launch contains no planned episodes")
        if mode == "live":
            with self._session() as db:
                prior = db.execute(
                    "SELECT 1 FROM launches WHERE experiment_id = ? AND mode = 'live' LIMIT 1",
                    (experiment.id,),
                ).fetchone()
                active = db.execute(
                    "SELECT 1 FROM launches WHERE experiment_id = ? AND mode = 'live' "
                    "AND status IN ('QUEUED', 'RUNNING', 'CANCELLING') LIMIT 1",
                    (experiment.id,),
                ).fetchone()
                if active:
                    raise ValueError("a live launch is still active; settle it before retrying")
                if prior and not force:
                    if design_configs is None:
                        raise ValueError("selective retry requires a locked design plan; use force for legacy YAML")
                    completed = self._successful_live_episodes(db, experiment.id)
                    design_configs = [config for config in design_configs
                                      if config["episode_id"] not in completed]
                    if not design_configs:
                        raise ValueError("all live episodes already completed; use force to rerun them")
        effective_shard_count = shard_count or 1
        if design_configs is None:
            planned = self._planned_attempts(experiment, smoke_test=mode == "smoke")
        else:
            planned = design_configs
        if effective_shard_count > len(planned):
            raise ValueError("shard_count cannot exceed planned episodes")

        if experiment.locked_at is not None and experiment.pinned_image_digest is None:
            raise ValueError("locked experiment has no pinned image identity")
        image_digest = (experiment.pinned_image_digest or None) if self.launcher.name == "local_container" else None
        grade = "verified" if image_digest else "unverified"
        from a2a_engine.agent_pool import load_agent_pool
        pool = load_agent_pool(self.workspace / "experiments")
        bindings = [str(name) for config in design_configs or []
                    for agent in config.get("agents", []) if isinstance(agent, dict)
                    for name in (agent.get("binding"), agent.get("model"))
                    if name in pool.agents]
        credentials = pool.required_credentials(bindings)
        if mode == "live":
            missing = pool.missing_credentials(bindings, self.credential_presence(credentials))
            if missing:
                raise ValueError(f"missing worker credentials: {', '.join(missing)}")
        launch_id = str(uuid.uuid4())
        launch = Launch(
            id=launch_id, experiment_id=experiment.id, status="QUEUED",
            max_parallelism=max_parallelism, trace_database=str(self.trace_database),
            created_at=_now(), mode=mode, shard_index=None,
            provenance_grade=grade, image_digest=image_digest,
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
                    config["provenance"]["run_mode"] = mode
                    config["provenance"]["provenance_grade"] = grade
                    config["provenance"]["image_digest"] = image_digest
                    attempt_rows.append((episode_id, cell_id, episode_idx, attempt))
            else:
                attempt_rows = [
                    (episode_id, cell_id, episode_idx, self._next_attempt(db, episode_id))
                    for episode_id, cell_id, episode_idx in planned
                ]

        if design_configs is not None:
            execution_path = self._write_execution_plan(launch.id, experiment, design_configs,
                                                       credentials=credentials)
            input_ref = self._publish_launch_input(
                launch.id, Path(execution_path).read_bytes()
            )
        else:
            # A legacy YAML experiment has no compiled plan to publish, so its
            # launch input is the reviewed workspace file itself -- referenced
            # in place, still digest-bound. Copying it would move it away from
            # the sidecars and agent pool it resolves against, which is a
            # behaviour change the boundary does not require.
            input_ref = self._reference_launch_input(experiment)
        launch = Launch(**{
            **asdict(launch),
            "execution_path": execution_path,
            "launch_input_uri": input_ref.uri,
            "launch_input_sha256": input_ref.sha256,
        })

        with self._session() as db:
            if mode == "live":
                db.execute("BEGIN IMMEDIATE")
                if db.execute(
                    "SELECT 1 FROM launches WHERE experiment_id = ? AND mode = 'live' "
                    "AND status IN ('QUEUED', 'RUNNING', 'CANCELLING') LIMIT 1",
                    (experiment.id,),
                ).fetchone():
                    raise ValueError("a live launch is still active; settle it before retrying")
                completed_now = self._successful_live_episodes(db, experiment.id) if not force else set()
                if design_configs is not None and any(
                    config["episode_id"] in completed_now for config in design_configs
                ):
                    raise ValueError("live results changed while planning; retry the launch")
                if any(self._next_attempt(db, episode_id) != attempt
                       for episode_id, _, _, attempt in attempt_rows):
                    raise ValueError("attempt numbers changed while planning; retry the launch")
            if design_bank is not None:
                self._ensure_items(
                    db,
                    design_bank,
                    {str(config["item_id"]) for config in design_configs or []},
                )
            db.execute(
                "INSERT INTO launches (id, experiment_id, status, max_parallelism, trace_database, "
                "mode, provenance_grade, image_digest, execution_path, launch_input_uri, launch_input_sha256, "
                "shard_index, shard_count, created_at, started_at, ended_at, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (launch.id, launch.experiment_id, launch.status, launch.max_parallelism,
                 launch.trace_database, launch.mode, launch.provenance_grade, launch.image_digest, launch.execution_path,
                 launch.launch_input_uri, launch.launch_input_sha256, launch.shard_index,
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
                "launch_input_uri": launch.launch_input_uri,
                "launch_input_sha256": launch.launch_input_sha256,
            })

        # Everything above committed before anything was dispatched, so a
        # process that dies here leaves a QUEUED launch the reconciler settles
        # rather than an execution nothing recorded.
        self._dispatch(launch, LaunchInputRef(
            uri=str(launch.launch_input_uri), sha256=str(launch.launch_input_sha256),
        ))
        return self.launch(launch.id)

    @staticmethod
    def _successful_live_episodes(db: sqlite3.Connection, experiment_id: str) -> set[str]:
        return {
            row[0] for row in db.execute(
                "SELECT DISTINCT a.episode_id FROM attempts a "
                "JOIN launches l ON l.id = a.launch_id "
                "JOIN episodes e ON e.episode_id = a.episode_id AND e.attempt = a.attempt "
                "WHERE l.experiment_id = ? AND l.mode = 'live' "
                "AND a.status = 'COMPLETED' AND e.status = 'COMPLETED' "
                "AND e.run_mode = 'live' AND e.experiment_id = ?",
                (experiment_id, experiment_id),
            ).fetchall()
        }

    def _dispatch(self, launch: Launch, input_ref: LaunchInputRef) -> None:
        """Hand the launch to the launcher and persist the handle it returns."""
        now = _now()
        with self._dispatching_lock:
            self._dispatching.add(launch.id)
        try:
            try:
                handle = self.launcher.submit(launch, input_ref)
            except Exception as exc:
                # Dispatch is on the identity path, not the observability one: a
                # launch that was never submitted must say so rather than sit in
                # QUEUED waiting for a worker that does not exist.
                with self._session() as db:
                    db.execute(
                        "UPDATE launches SET status = ?, ended_at = ?, error = ?, "
                        "provenance_grade = 'unverified', image_digest = NULL WHERE id = ?",
                        ("FAILED", now, f"dispatch failed: {exc}", launch.id),
                    )
                    db.execute(
                        "UPDATE attempts SET status = ?, ended_at = ?, error = ? "
                        "WHERE launch_id = ? AND status = ?",
                        ("FAILED", now, f"dispatch failed: {exc}", launch.id, "QUEUED"),
                    )
                    self._event(db, launch.id, "launch.failed", {"error": f"dispatch failed: {exc}"})
                raise
            with self._session() as db:
                db.execute(
                    "UPDATE launches SET execution_handle = ?, status = ?, started_at = ? WHERE id = ?",
                    (handle.to_json(), "RUNNING", now, launch.id),
                )
                db.execute(
                    "UPDATE attempts SET status = ?, started_at = ? WHERE launch_id = ? AND status = ?",
                    ("RUNNING", now, launch.id, "QUEUED"),
                )
                self._event(db, launch.id, "launch.submitted", {
                    "backend": handle.backend, "handle": handle.id,
                })
        finally:
            # Released only once the handle is durable (or the failure is), so
            # the reconciler never observes the gap between the two.
            with self._dispatching_lock:
                self._dispatching.discard(launch.id)

    def resume_attempt(self, launch_id: str, episode_id: str) -> Launch:
        """Dispatch one new experimental attempt from a settled partial trace."""
        source = self.launch(launch_id)
        if source.status in {"QUEUED", "RUNNING", "CANCELLING"} or source.mode != "live":
            raise ValueError("only a settled live launch can be resumed")
        experiment = self.experiment(source.experiment_id)
        if experiment.design_text is None or experiment.locked_at is None:
            raise ValueError("resume requires a locked design with a frozen episode plan")
        with self._session() as db:
            attempt = db.execute(
                "SELECT * FROM attempts WHERE launch_id = ? AND episode_id = ?",
                (launch_id, episode_id),
            ).fetchone()
        if attempt is None or attempt["status"] not in {"UNREPORTED", "FAILED", "CANCELLED"} or not attempt["episode_uri"]:
            raise ValueError("the selected attempt has no recovered PARTIAL episode")
        source_attempt = self._attempt(attempt)
        refs = list(self.artifacts.iter_event_sinks(launch_id, episode_id=episode_id))
        if len(refs) != 1:
            raise ValueError("resume requires exactly one durable source artifact")
        ref = refs[0]
        if source_attempt.episode_uri != self._store().uri(ref.episode_uid):
            raise ValueError("resume source does not match the selected attempt")
        trace = self._store().get_episode(ref.episode_uid)
        if trace is None or not trace.observability.get("partial") or (
            trace.observability.get("durable_through") != ref.observed_through()
        ):
            raise ValueError("resume requires the recovered PARTIAL trace and its durable source")
        entries = read_event_sink(ref)
        if not entries or ref.observed_through() != len(entries):
            raise ValueError("resume artifact does not match its durable high-water mark")
        from a2a_engine.llm.replay import ReplayingClient
        ReplayingClient(entries)
        configs = [cfg for cfg in self._design_episode_configs(experiment, mode="live")
                   if cfg["episode_id"] == episode_id]
        if len(configs) != 1:
            raise ValueError("episode is not uniquely present in the frozen design")
        config = configs[0]
        from a2a_engine.agent_pool import load_agent_pool
        pool = load_agent_pool(self.workspace / "experiments")
        bindings = [str(name) for agent in config.get("agents", []) if isinstance(agent, dict)
                    for name in (agent.get("binding"), agent.get("model")) if name in pool.agents]
        credentials = pool.required_credentials(bindings)
        missing = pool.missing_credentials(bindings, self.credential_presence(credentials))
        if missing:
            raise ValueError(f"missing worker credentials: {', '.join(missing)}")
        with self._session() as db:
            next_attempt = self._next_attempt(db, episode_id)
        if experiment.pinned_image_digest is None:
            raise ValueError("locked experiment has no pinned image identity")
        image_digest = (experiment.pinned_image_digest or None) if self.launcher.name == "local_container" else None
        if image_digest and source.image_digest != image_digest:
            raise ValueError("cannot resume a source attempt from a different execution image")
        grade = "verified" if image_digest else "unverified"
        config["provenance"] = {**config["provenance"], "attempt": next_attempt,
                                "run_mode": "live", "provenance_grade": grade,
                                "image_digest": image_digest}
        config["_resume_from"] = {
            "uri": ref.uri, "sha256": sha256_bytes(ref.read_bytes()),
            "durable_through": len(entries), "episode_id": episode_id,
        }
        new_id = str(uuid.uuid4())
        path = self._write_execution_plan(new_id, experiment, [config], credentials=credentials)
        input_ref = self._publish_launch_input(new_id, Path(path).read_bytes())
        launch = Launch(
            id=new_id, experiment_id=experiment.id, status="QUEUED",
            max_parallelism=1, trace_database=str(self.trace_database), created_at=_now(),
            mode="live", shard_count=1, execution_path=path,
            provenance_grade=grade, image_digest=image_digest,
            launch_input_uri=input_ref.uri, launch_input_sha256=input_ref.sha256,
        )
        with self._session() as db:
            db.execute(
                "INSERT INTO launches (id, experiment_id, status, max_parallelism, trace_database, "
                "mode, provenance_grade, image_digest, execution_path, launch_input_uri, launch_input_sha256, shard_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (launch.id, launch.experiment_id, launch.status, launch.max_parallelism,
                 launch.trace_database, launch.mode, launch.provenance_grade, launch.image_digest, launch.execution_path,
                 launch.launch_input_uri, launch.launch_input_sha256, launch.shard_count, launch.created_at),
            )
            db.execute(
                "INSERT INTO attempts (id, launch_id, episode_id, cell_id, episode_idx, attempt, status) "
                "VALUES (?, ?, ?, ?, ?, ?, 'QUEUED')",
                (str(uuid.uuid4()), new_id, episode_id, source_attempt.cell_id,
                 source_attempt.episode_idx, next_attempt),
            )
            self._event(db, new_id, "launch.queued", {
                "resume_of": launch_id, "episode_id": episode_id, "source_episode_uid": ref.episode_uid,
            })
        self._dispatch(launch, LaunchInputRef(uri=input_ref.uri, sha256=input_ref.sha256))
        return self.launch(new_id)

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
                design, self._design_declaration(release), self._item_bank(release.environment_id),
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
        self, launch_id: str, experiment: Experiment, configs: list[dict[str, Any]],
        *, credentials: list[str] | None = None,
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
            yaml.safe_dump({"name": experiment.name, "cells": cells,
                            "credentials": credentials or []}, sort_keys=False),
            encoding="utf-8",
        )
        return str(path)

    def _publish_launch_input(self, launch_id: str, plan_bytes: bytes) -> ArtifactRef:
        """Publish the rendered plan as the launch's addressable input.

        The digest binds the plan's *own* bytes, deliberately not the
        experiment's current ``design_sha256``. Role normalization can rewrite
        a locked design's text and digest while the frozen
        ``episode_configs[].provenance.design_sha256`` keeps the authored one,
        so the two may legitimately differ and nothing downstream may assume
        they agree.
        """
        return self.artifacts.put(plan_key(launch_id), plan_bytes)

    def _reference_launch_input(self, experiment: Experiment) -> ArtifactRef:
        """Reference a legacy experiment YAML in place, bound to its digest.

        Nothing re-validated this file's bytes between registration and
        execution before; now the worker refuses to parse it unless it still
        hashes to what the launch recorded.
        """
        path = self._workspace_path(experiment.yaml_path)
        return ArtifactRef(uri=path.as_uri(), sha256=sha256_bytes(path.read_bytes()))

    def cancel_launch(self, launch_id: str) -> Launch:
        """Request termination; the reconciler settles the final status.

        ``CANCELLING`` is a genuinely transient state rather than a claim: a
        cancelled shard's in-flight episode is still recovered as ``PARTIAL``,
        because recording partial evidence is strictly more information than
        discarding it.
        """
        launch = self.launch(launch_id)
        if launch.status not in {"QUEUED", "RUNNING"}:
            return launch
        handle = ExecutionHandle.from_json(launch.execution_handle)
        if handle is not None and self.launcher.cancel(handle):
            with self._session() as db:
                db.execute("UPDATE launches SET status = ? WHERE id = ?", ("CANCELLING", launch_id))
                self._event(db, launch_id, "launch.cancelling", {})
        # Deliberately not a reconciling read: a cancel request reports that it
        # was accepted, and the next read settles the final status from the
        # evidence the worker left behind.
        return self._launch_row(launch_id)

    def launch(self, launch_id: str) -> Launch:
        # Every read that asks about a launch first gives the reconciler a
        # chance to advance it. Reconciliation is the only writer of launch
        # status, so a read is where an unattended local stack learns that a
        # worker exited -- no background thread, no cadence to tune.
        self.reconciler.tick()
        return self._launch_row(launch_id)

    def _launch_row(self, launch_id: str) -> Launch:
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
                SELECT a.*, winner.episode_uid, winner.execution
                FROM attempts a
                LEFT JOIN episodes winner ON winner.episode_uid = (
                    SELECT e.episode_uid
                    FROM episodes e
                    WHERE e.episode_id = a.episode_id AND e.attempt = a.attempt
                    ORDER BY e.attempt DESC, e.execution DESC
                    LIMIT 1
                )
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
                "execution": row["execution"],
                "redis_stream": prefix + row["episode_id"],
            } for row in attempts],
            "progress": self._progress_rows(launch_id),
            # Every launch event, not only the parsed stdout lines that used to
            # be the only kind here. The worker's stdout belongs to its
            # platform now; what a researcher reopens is the control plane's
            # own durable record of what it did and what it then observed.
            "runner_logs": self.events(launch_id),
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
        """Progress as an identity join over this launch's planned attempts.

        The whole fan-out is materialised before any episode runs, and the
        episode store answers "which of these ``(episode_id, attempt)`` pairs
        do you hold completed" -- the same question settlement asks. When both
        tables live in one database that is one query, and because it reads
        persisted state rather than a process's stdout it survives a server
        restart unchanged.

        A ``PARTIAL`` episode is a recovered fragment, not a completed run, so
        it is deliberately not counted.
        """
        self.reconciler.tick()
        return self._progress_rows(launch_id)

    def _progress_rows(self, launch_id: str) -> dict[str, Any]:
        if self.colocated_reads:
            return self._progress_rows_sql(launch_id)
        return self._progress_rows_two_step(launch_id)

    def _progress_rows_two_step(self, launch_id: str) -> dict[str, Any]:
        """The reference: plan from here, evidence from the store, join in memory.

        Attempt-level, exactly like settlement. A launch's progress is a claim
        about the attempts *it* planned: the dispatched worker never runs with
        ``--resume``, so it re-executes every one of them, and an earlier
        launch's completed episode for the same id is not work this launch has
        done. Crediting it made a re-launch read ``2/2`` while both of its
        attempts were still running.
        """
        with self._session() as db:
            rows = db.execute(
                "SELECT cell_id, episode_id, attempt, status FROM attempts "
                "WHERE launch_id = ? ORDER BY cell_id",
                (launch_id,),
            ).fetchall()
        planned = sorted({row["episode_id"] for row in rows})
        done = set(self._store().completed_executions(planned))
        by_cell: dict[str, dict[str, Any]] = {}
        for row in rows:
            cell = by_cell.setdefault(
                row["cell_id"],
                {"cell_id": row["cell_id"], "planned": 0, "completed": 0, "failed": 0},
            )
            cell["planned"] += 1
            # ``DISTINCT`` in the SQL form: a cell counts an episode once
            # however many rows the store holds for it.
            cell["completed"] += 1 if (row["episode_id"], row["attempt"]) in done else 0
            cell["failed"] += 1 if row["status"] == "FAILED" else 0
        return self._progress_totals([by_cell[key] for key in sorted(by_cell)])

    def _progress_rows_sql(self, launch_id: str) -> dict[str, Any]:
        with self._session() as db:
            rows = db.execute(
                """
                SELECT a.cell_id AS cell_id,
                       COUNT(*) AS planned,
                       SUM(CASE WHEN done.episode_id IS NOT NULL THEN 1 ELSE 0 END) AS completed,
                       SUM(CASE WHEN a.status = 'FAILED' THEN 1 ELSE 0 END) AS failed
                FROM attempts a
                LEFT JOIN (
                    SELECT DISTINCT episode_id, attempt FROM episodes WHERE status = 'COMPLETED'
                ) done ON done.episode_id = a.episode_id AND done.attempt = a.attempt
                WHERE a.launch_id = ?
                GROUP BY a.cell_id
                ORDER BY a.cell_id
                """,
                (launch_id,),
            ).fetchall()
        return self._progress_totals([
            {"cell_id": row["cell_id"], "planned": row["planned"],
             "completed": row["completed"], "failed": row["failed"]}
            for row in rows
        ])

    @staticmethod
    def _progress_totals(by_cell: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "by_cell": by_cell,
            "planned": sum(cell["planned"] for cell in by_cell),
            "completed": sum(cell["completed"] for cell in by_cell),
            "failed": sum(cell["failed"] for cell in by_cell),
        }

    # --- launch truth, written twice on purpose ---------------------------
    #
    # Two queries define whether a launch succeeded, and both are a join of
    # what the control plane planned against what the episode store holds.
    # Written as one SQL statement they are correct and fast and quietly
    # require the two tables to share a file -- which would make an
    # object-store episode backend impossible to adopt later without rewriting
    # the definition of launch truth. So each is written both ways: the
    # two-step is the reference, the single statement is an optimization that
    # applies when the store happens to be co-located, and a test asserts they
    # agree.

    def completed_executions(self, launch_id: str) -> dict[tuple[str, int], str]:
        """``(episode_id, attempt) -> episode_uid`` for this launch's episodes."""
        if self.colocated_reads:
            return self._completed_executions_sql(launch_id)
        return self._completed_executions_two_step(launch_id)

    def _completed_executions_sql(self, launch_id: str) -> dict[tuple[str, int], str]:
        """One statement, co-located: ``attempts`` joined to ``episodes``."""
        with self._session() as db:
            rows = db.execute(
                """
                SELECT a.episode_id AS episode_id, a.attempt AS attempt,
                       e.episode_uid AS episode_uid
                FROM attempts a
                JOIN episodes e
                  ON e.episode_id = a.episode_id
                 AND e.attempt = a.attempt
                 AND e.status = 'COMPLETED'
                WHERE a.launch_id = ?
                ORDER BY a.attempt, e.execution
                """,
                (launch_id,),
            ).fetchall()
        return {
            (row["episode_id"], int(row["attempt"])): row["episode_uid"] for row in rows
        }

    def _completed_executions_two_step(self, launch_id: str) -> dict[tuple[str, int], str]:
        """Two steps, no co-location required.

        Ask the control plane what it planned, ask the episode store which of
        those ids it holds a completed run for, and join in memory. This is the
        shape ``server._lanes`` already uses to put a roster beside a trace, and
        it is the only one that survives the two stores being different things.
        """
        with self._session() as db:
            planned = [
                (row["episode_id"], int(row["attempt"]))
                for row in db.execute(
                    "SELECT episode_id, attempt FROM attempts WHERE launch_id = ?",
                    (launch_id,),
                ).fetchall()
            ]
        held = self._store().completed_executions(sorted({eid for eid, _ in planned}))
        return {key: held[key] for key in planned if key in held}

    def _experiment_name(self, launch: Launch) -> str:
        """The experiment name a recovered episode's manifest is filed under.

        Read from the control plane's own record rather than from a directory
        name on the worker's disk, which is the last thing recovery used the
        shared filesystem for.
        """
        with self._session() as db:
            row = db.execute(
                "SELECT name FROM experiments WHERE id = ?", (launch.experiment_id,)
            ).fetchone()
        return str(row["name"]) if row is not None else ""

    def _open_launches(self) -> list[Launch]:
        """Every launch the reconciler is still responsible for."""
        with self._session() as db:
            rows = db.execute(
                "SELECT * FROM launches WHERE status IN ('QUEUED', 'RUNNING', 'CANCELLING') "
                "ORDER BY created_at"
            ).fetchall()
        with self._dispatching_lock:
            in_flight = frozenset(self._dispatching)
        return [self._launch(row) for row in rows if row["id"] not in in_flight]

    def reconcile(self) -> list[str]:
        """Advance every open launch once. Kept as the public spelling of a tick."""
        return self.reconciler.tick()

    def durable_progress(self, launch: Launch) -> list[dict[str, Any]]:
        """What the durable record says about each unsettled attempt, right now.

        Progress is *derived*, never reported. The worker pushes nothing here
        and holds no control-plane credential; what moves a counter is an event
        that reached the shared artifact store, plus the episodes fact table
        for the one transition a stream cannot announce about itself -- that it
        persisted a complete episode.

        An attempt with no evidence at all is omitted rather than reported at
        zero. "Nothing has happened yet" is what the planned attempt row
        already says, and emitting it as progress would make a queue look like
        a queue that is moving.
        """
        with self._session() as db:
            attempts = [
                self._attempt(row) for row in db.execute(
                    "SELECT * FROM attempts WHERE launch_id = ? "
                    "AND status IN ('QUEUED', 'RUNNING')",
                    (launch.id,),
                ).fetchall()
            ]
        if not attempts:
            return []
        completed = self.completed_executions(launch.id)
        marks: dict[str, tuple[int, str]] = {}
        try:
            for ref in self.artifacts.iter_event_sinks(launch.id):
                through = ref.observed_through()
                previous = marks.get(ref.episode_id)
                # A retried episode publishes under a second uid, so the
                # furthest-along stream is the one that describes this attempt.
                if previous is None or through > previous[0]:
                    marks[ref.episode_id] = (through, ref.episode_uid)
        except Exception:  # pragma: no cover - defensive
            # Enumeration is an observability read: it degrades to "no news"
            # rather than stopping the pass that also settles launches.
            log.warning("could not enumerate durable evidence for %s", launch.id, exc_info=True)
        rows: list[dict[str, Any]] = []
        for attempt in attempts:
            through, uid = marks.get(attempt.episode_id, (0, ""))
            episode_uid = completed.get((attempt.episode_id, attempt.attempt))
            if episode_uid is None and not through:
                continue
            rows.append({
                "episode_id": attempt.episode_id,
                "attempt": attempt.attempt,
                "status": "COMPLETED" if episode_uid is not None else "RUNNING",
                "durable_through": through,
                "episode_uid": episode_uid or uid or None,
            })
        return rows

    def record_attempt_progress(self, launch_id: str, rows: list[dict[str, Any]]) -> None:
        """Append derived progress to the one durable log the SSE stream reads.

        ``launch_events`` is already the single source the browser sees, so a
        progress frame is the same kind of row as ``launch.settled`` and
        survives a reload for free. Nothing here resolves an attempt: settling
        stays the reconciler's terminal step, and a live ``COMPLETED`` frame is
        a statement about evidence, not a status transition.
        """
        if not rows:
            return
        with self._session() as db:
            for row in rows:
                self._event(db, launch_id, "attempt.progress", row)

    def _settle_from_evidence(self, launch: Launch, *,
                              execution: ExecutionStatus = ExecutionStatus.UNKNOWN) -> None:
        """Settle a launch by joining ``attempts`` against the fact table.

        ``execution`` is the launcher's liveness hint. It never decides whether
        an episode exists -- only the join does that -- and it is consulted for
        exactly two things the evidence genuinely cannot answer: whether a stop
        was requested, and how to *describe* an attempt that left no trace at
        all. An attempt whose worker claimed success but persisted nothing
        still lands ``UNREPORTED``, which is the gap staying visible rather
        than a result being invented.
        """
        cancelled = launch.status == "CANCELLING" or execution is ExecutionStatus.CANCELLED
        # A dry run validates reachability and deliberately writes no trace, so
        # there is no evidence to join against and its absence is the expected
        # outcome rather than a gap.
        dry_run = launch.mode == "dry_run" and execution is ExecutionStatus.SUCCEEDED
        now = _now()
        with self._session() as db:
            attempts = [
                self._attempt(row) for row in db.execute(
                    "SELECT * FROM attempts WHERE launch_id = ? AND status IN ('QUEUED', 'RUNNING')",
                    (launch.id,),
                ).fetchall()
            ]
        completed = self.completed_executions(launch.id)
        experiment_name = self._experiment_name(launch)
        recovered: list[str] = []
        unreported: list[str] = []
        for attempt in attempts:
            episode_uid = completed.get((attempt.episode_id, attempt.attempt))
            if episode_uid is not None:
                self._resolve_attempt(
                    attempt, status="COMPLETED",
                    episode_uri=self._store().uri(episode_uid), error=None, ended_at=now,
                )
                continue
            if dry_run:
                self._resolve_attempt(
                    attempt, status="DRY_RUN", episode_uri=None, error=None, ended_at=now,
                )
                continue
            partial_uri = self._recover_partial(attempt, experiment_name=experiment_name)
            if partial_uri is not None:
                recovered.append(attempt.episode_id)
            else:
                unreported.append(attempt.episode_id)
            self._resolve_attempt(
                attempt,
                status=(
                    "CANCELLED" if cancelled
                    else "FAILED" if execution is ExecutionStatus.FAILED
                    else "UNREPORTED"
                ),
                episode_uri=partial_uri,
                error=_attempt_diagnostic(execution, cancelled=cancelled, partial=bool(partial_uri)),
                ended_at=now,
            )
        with self._session() as db:
            outstanding = db.execute(
                "SELECT COUNT(*) AS n FROM attempts WHERE launch_id = ? "
                "AND status NOT IN ('COMPLETED', 'DRY_RUN')",
                (launch.id,),
            ).fetchone()["n"]
            status = "CANCELLED" if cancelled else ("COMPLETED" if not outstanding else "FAILED")
            db.execute(
                "UPDATE launches SET status = ?, ended_at = ?, error = ? WHERE id = ?",
                (status, now, None if status == "COMPLETED" else _launch_diagnostic(execution),
                 launch.id),
            )
            if unreported:
                self._event(db, launch.id, "launch.unreported_episodes",
                            {"episode_ids": unreported})
            self._event(db, launch.id, "launch.settled", {
                "status": status, "execution_status": execution.value,
                "recovered_episode_ids": recovered,
            })
            self._event(db, launch.id, f"launch.{status.lower()}", {
                "execution_status": execution.value,
            })

    def _resolve_attempt(self, attempt: Attempt, *, status: str, episode_uri: str | None,
                         error: str | None, ended_at: str) -> None:
        with self._session() as db:
            db.execute(
                "UPDATE attempts SET status = ?, episode_uri = ?, error = ?, ended_at = ? "
                "WHERE id = ?",
                (status, episode_uri, error, ended_at, attempt.id),
            )

    def _recover_partial(self, attempt: Attempt, *, experiment_name: str = "") -> str | None:
        """Persist whatever the interrupted episode's event log holds.

        Every entry was flushed locally and published to the shared artifact
        store as it was written, so a run killed between two events still has
        everything up to that point -- and the control plane finds it by
        enumerating a prefix it owns rather than by reading the worker's
        filesystem, which it no longer can. The recovered trace is stored with
        ``status = PARTIAL``: it is evidence for inspection and retry, never a
        successful experimental result.
        """
        for ref in self.artifacts.iter_event_sinks(
            attempt.launch_id, episode_id=attempt.episode_id
        ):
            entries = read_event_sink(ref)
            if not entries or entries[0].get("episode_id") != attempt.episode_id:
                continue
            try:
                trace = project_events_to_trace(ref)
            except ValueError:
                continue
            # How far durability actually reached, so a PARTIAL says what it is
            # rather than presenting a truncated event list as the whole story.
            # An absent watermark is the SIGKILL case: the sink never got to
            # say, and what is in the artifact is what was durable.
            trace.observability["durable_through"] = (
                ref.watermark if ref.watermark is not None else len(entries)
            )
            trace.observability["durable_closed"] = ref.closed
            # The event log carries events, not identity: a projected trace has
            # no provenance, so it was stored as attempt 1 with no experiment,
            # release, seed or roster. Re-attach what the worker was told to
            # run, so the fragment files under the attempt that produced it.
            trace.config = type(trace.config).model_validate({
                **trace.config.model_dump(),
                **self._planned_identity(attempt),
            })
            manifest = EpisodeManifest.from_run(
                config=trace.config.model_dump(),
                experiment_name=experiment_name,
                cell_id=attempt.cell_id,
                episode_idx=attempt.episode_idx,
                episode_uid=trace.episode_uid,
            )
            manifest.episode_id = attempt.episode_id
            manifest.attempt = attempt.attempt
            manifest.environment_id = str(trace.config.environment_id or "")
            return self._store().put_episode(trace, manifest)
        return None

    def _planned_identity(self, attempt: Attempt) -> dict[str, Any]:
        """The identity a worker stamps on an episode, read from its launch input.

        The launch input is the digest-bound plan the worker verified and ran,
        so it is the authority on what this attempt *was* -- the same
        provenance and seed the runner copies onto a completed episode. A
        legacy launch has no compiled plan to read; it still gets the one fact
        the control plane owns, its attempt number.
        """
        fallback = {"provenance": {
            "attempt": attempt.attempt, "cell_id": attempt.cell_id,
            "episode_idx": attempt.episode_idx,
        }}
        with self._session() as db:
            row = db.execute(
                "SELECT launch_input_uri, launch_input_sha256 FROM launches WHERE id = ?",
                (attempt.launch_id,),
            ).fetchone()
        if row is None or not row["launch_input_uri"]:
            return fallback
        try:
            plan = yaml.safe_load(fetch_verified(ArtifactRef(
                uri=row["launch_input_uri"], sha256=row["launch_input_sha256"],
            ))) or {}
        except Exception as exc:  # the fragment is still worth keeping
            log.warning("launch input unreadable for %s: %s", attempt.launch_id, exc)
            return fallback
        for cell in plan.get("cells") or []:
            planned = (cell.get("config") or {}).get("_design_episode")
            if isinstance(planned, dict) and planned.get("episode_id") == attempt.episode_id:
                return {
                    "episode_id": attempt.episode_id,
                    "seed": int(planned["seed"]),
                    "provenance": dict(planned["provenance"]),
                }
        return fallback

    def live_trace(self, episode_uid: str) -> EpisodeTrace | None:
        """An in-flight episode, projected from its durable event stream.

        The episode store holds nothing for an episode until its worker
        persists it, but every event it has emitted so far is already in the
        artifact store -- the same evidence ``_recover_partial`` reads. This
        projects it on demand and persists nothing: a live view is a read, and
        whatever the episode becomes is still settled from evidence by the
        reconciler. Only open launches are searched, because once a launch
        settles the store holds the episode, completed or recovered.
        """
        for launch in self._open_launches():
            for ref in self.artifacts.iter_event_sinks(launch.id):
                if ref.episode_uid != episode_uid:
                    continue
                try:
                    trace = project_events_to_trace(ref)
                except ValueError:
                    # Not even a first event yet: known, but nothing to show.
                    return None
                trace.observability["durable_through"] = ref.observed_through()
                trace.observability["live"] = True
                return trace
        return None

    @staticmethod
    def _next_attempt(db: sqlite3.Connection, episode_id: str) -> int:
        row = db.execute(
            "SELECT COALESCE(MAX(attempt), 0) + 1 AS next FROM attempts WHERE episode_id = ?",
            (episode_id,),
        ).fetchone()
        return int(row["next"])

    def launches(self) -> list[Launch]:
        self.reconciler.tick()
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
        attempts: list[tuple[str, str, int]] = []
        for cell, _ in expand_cells(spec):
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
        declaration = self._design_declaration(self._release_for(environment_id, None))
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

    @staticmethod
    def _ensure_items(db: sqlite3.Connection, bank: ItemBank, item_ids: set[str]) -> None:
        """Synchronize only launch-selected rows missing from or stale in SQLite."""
        if not item_ids:
            return
        selected_items = {item_id: bank.get(item_id) for item_id in item_ids}
        existing_hashes: dict[str, str] = {}
        # SQLite limits bound variables (often to 999), while a full design may
        # select more items than fit in one IN clause.
        item_id_list = sorted(selected_items)
        for start in range(0, len(item_id_list), 900):
            batch = item_id_list[start:start + 900]
            placeholders = ", ".join("?" for _ in batch)
            existing_hashes.update({
                str(row["item_id"]): str(row["item_bank_sha256"])
                for row in db.execute(
                    f"SELECT item_id, item_bank_sha256 FROM items "
                    f"WHERE item_id IN ({placeholders})",
                    batch,
                )
            })
        stale_or_missing = [
            selected_items[item_id]
            for item_id in item_id_list
            if existing_hashes.get(item_id) != bank.item_bank_sha256
        ]
        if not stale_or_missing:
            return
        db.executemany(
            "INSERT INTO items (item_id, item_bank_sha256, params, oracle_result) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(item_id) DO UPDATE SET item_bank_sha256 = excluded.item_bank_sha256, "
            "params = excluded.params, oracle_result = excluded.oracle_result",
            [
                (item.item_id, bank.item_bank_sha256, _json(item.params), _json(item.oracle_result))
                for item in stale_or_missing
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
            row["image_digest"], json.loads(row["manifest"]) if row["manifest"] else None,
        )

    @staticmethod
    def _experiment(row: sqlite3.Row) -> Experiment:
        values = dict(row)
        if values.get("role_normalization"):
            values["role_normalization"] = json.loads(values["role_normalization"])
        return Experiment(**values)

    @staticmethod
    def _launch(row: sqlite3.Row) -> Launch:
        return Launch(**dict(row))

    @staticmethod
    def _attempt(row: sqlite3.Row) -> Attempt:
        values = dict(row)
        return Attempt(**{field: values[field] for field in Attempt.__dataclass_fields__})
