"""SQLite episode store — the lightweight "local DB" sink.

This is the zero-setup option: no bucket, no GCP project, no credentials. A new
contributor points ``storage.path`` at a file and gets a queryable corpus of
runs they can open with any SQLite client.

Unlike :mod:`a2a_engine.storage.s3` and :mod:`a2a_engine.storage.firestore`,
which mirror a local JSON tree to a remote and treat the remote as best-effort,
SQLite *is* the ground truth when selected. Writing both a JSON tree and a
database would double the on-disk footprint and leave two records that can
disagree. Set ``mirror_json: true`` if you want the JSON tree as well.

The DDL lives in :mod:`a2a_engine.storage.schema`, which the control plane
executes too: ``episodes`` is the fact table of one star schema in one file, so
a fact row joins its dimensions in SQL rather than through an application-side
lookup. Writing an episode also projects the release dimension out of its own
provenance block, so a runner invoked with nothing but ``--storage-path``
produces a database that says which release produced each row.

``episode_id`` is indexed rather than unique: re-running an experiment after a
crash legitimately produces a second attempt at the same run id, and
``--resume`` is what decides whether to skip it.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from a2a_engine.derived import DerivedArtifact
from a2a_engine.manifest import EpisodeManifest
from a2a_engine.provenance import promoted_columns, release_dimension
from a2a_engine.ratings.schemas import RatingEvent, RatingSnapshot
from a2a_engine.schemas import EpisodeTrace
from a2a_engine.storage.base import StoreCheck, register_store
from a2a_engine.storage.local import LocalJSONStore
from a2a_engine.storage.schema import apply_schema

log = logging.getLogger("a2a_engine.storage.sqlite")

DEFAULT_DB_NAME = "a2a.db"

# Columns a caller may filter list_episodes() on. Restricting to real columns
# keeps the filter clause parameterised and prevents a filter key from being
# interpolated into SQL.
_FILTERABLE = {
    "episode_uid", "environment_id", "experiment_name", "episode_id",
    "cell_id", "episode_idx", "stopped",
    "experiment_id", "release_id", "item_id", "attempt", "seed", "status",
}

# The identity and provenance columns a list view needs. Selecting them by name
# is what keeps the episode list off the eight-``json.loads``-per-row path the
# unpaginated full scan used to take.
_SUMMARY_COLUMNS = (
    "episode_uid", "environment_id", "experiment_name", "episode_id",
    "cell_id", "episode_idx", "experiment_id", "release_id", "item_id",
    "attempt", "seed", "status", "started_at", "ended_at", "stopped", "created_at",
)


class SQLiteEpisodeStore:
    """Persists episodes into a single SQLite database file."""

    name = "sqlite"

    def __init__(
        self,
        *,
        path: str | Path | None = None,
        results_dir: str | Path = "./results",
        mirror_json: bool = False,
        timeout: float = 30.0,
        **_ignored: Any,
    ) -> None:
        self.results_dir = Path(results_dir)
        self.path = Path(path) if path else self.results_dir / DEFAULT_DB_NAME
        self.timeout = timeout
        # Opt-in JSON mirror for anyone whose downstream tooling still globs the
        # results tree. Off by default so the DB is the single record.
        self.local = LocalJSONStore(results_dir=results_dir) if mirror_json else None
        self._write_lock = threading.Lock()
        self._initialised = False

    # --- connection plumbing ---

    def _connect(self) -> sqlite3.Connection:
        """Open a short-lived connection.

        The runner fans out across a thread pool and SQLite connections are not
        safe to share between threads, so each operation gets its own. WAL mode
        lets concurrent readers proceed while a writer holds the lock.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=self.timeout)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        # One database means the fact table's references to its dimensions are
        # real constraints rather than a convention. SQLite enforces them per
        # connection, so both owners have to ask.
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        if self._initialised:
            return
        apply_schema(conn)
        self._initialised = True

    def uri(self, episode_uid: str = "") -> str:
        base = f"sqlite:///{self.path.resolve()}"
        return f"{base}#{episode_uid}" if episode_uid else base

    # --- write ---

    def put_episode(self, trace: EpisodeTrace, manifest: EpisodeManifest) -> str:
        payload = json.loads(trace.model_dump_json())
        events = payload.get("events", [])

        if self.local is not None:
            # Mirror first so local_trace_path/trace_size_bytes are populated in
            # the manifest we are about to store.
            self.local.put_episode(trace, manifest)

        manifest.storage.backend = self.name
        # Promotion reads the provenance block the compiler stamped; the block
        # itself stays inside ``config`` and is the durable copy.
        promoted = promoted_columns(trace)
        row = (
            trace.episode_uid,
            manifest.environment_id or payload.get("config", {}).get("environment_id"),
            manifest.experiment_name,
            manifest.episode_id,
            manifest.cell_id,
            manifest.episode_idx,
            promoted["experiment_id"],
            promoted["release_id"],
            promoted["item_id"],
            promoted["attempt"],
            promoted["seed"] if promoted["seed"] is not None else manifest.seed,
            promoted["status"],
            json.dumps(payload.get("config", {})),
            json.dumps(events),
            json.dumps(payload.get("final_state", {})),
            json.dumps(payload.get("metrics", {})),
            json.dumps(payload.get("release") or {}),
            json.dumps(payload.get("episode") or {}),
            json.dumps(payload.get("observability", {})),
            payload.get("started_at"),
            payload.get("ended_at"),
            1 if payload.get("stopped") else 0,
        )

        try:
            with self._write_lock:
                conn = self._connect()
                try:
                    self._ensure_schema(conn)
                    self._project_release(conn, trace)
                    # The manifest is written last and includes the status we are
                    # about to commit, so serialise it after the row is staged.
                    manifest.storage.uri = self.uri(trace.episode_uid)
                    manifest.storage.status = "written"
                    conn.execute(
                        "INSERT OR REPLACE INTO episodes ("
                        "  episode_uid, environment_id, experiment_name, episode_id,"
                        "  cell_id, episode_idx, experiment_id, release_id, item_id,"
                        "  attempt, seed, status,"
                        "  config, events, final_state, metrics, release, episode,"
                        "  observability, started_at, ended_at, stopped, manifest"
                        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (*row, manifest.model_dump_json()),
                    )
                    # Replacing a trace means its source payload changed; any
                    # prior analysis result for this environment is no longer valid.
                    conn.execute("DELETE FROM derived_artifacts WHERE episode_uid = ?", (trace.episode_uid,))
                    conn.execute("DELETE FROM rating_events WHERE episode_uid = ?", (trace.episode_uid,))
                    conn.commit()
                finally:
                    conn.close()
        except Exception as exc:
            manifest.storage.status = "failed"
            manifest.storage.error = f"{type(exc).__name__}: {exc}"
            log.error("SQLite write failed for %s: %s", manifest.episode_id, exc)
            if self.local is not None:
                # The JSON mirror is the surviving copy; hand back that path.
                self.local.write_manifest(manifest, Path(manifest.local_trace_path))
                return manifest.local_trace_path or self.uri()
            raise

        if self.local is not None:
            self.local.write_manifest(manifest, Path(manifest.local_trace_path))
        return manifest.storage.uri or self.uri(trace.episode_uid)

    @staticmethod
    def _project_release(conn: sqlite3.Connection, trace: EpisodeTrace) -> None:
        """Ensure the release this episode names exists as a dimension row.

        First writer wins: the control plane seeds the same row when it knows
        about the installed release, and a bare runner projects it out of the
        trace. Both describe one content-addressed release, so neither should
        overwrite the other's description of it.
        """
        dimension = release_dimension(trace)
        if dimension is None:
            return
        conn.execute(
            "INSERT OR IGNORE INTO releases "
            "(id, environment_id, version, declaration_sha256, item_bank_sha256,"
            " oracle_version, source_ref) VALUES (?,?,?,?,?,?,?)",
            (
                dimension["id"], dimension["environment_id"], dimension["version"],
                dimension["declaration_sha256"], dimension["item_bank_sha256"],
                dimension["oracle_version"], "episode-projection",
            ),
        )

    # --- read ---

    def get_episode(self, episode_uid: str) -> EpisodeTrace | None:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT * FROM episodes WHERE episode_uid = ?", (episode_uid,)
            ).fetchone()
        finally:
            conn.close()
        return self._row_to_trace(row) if row is not None else None

    def list_episodes(
        self,
        filters: dict[str, Any] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        where, params = self._where(filters)
        offset = int(cursor) if cursor else 0
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            rows = conn.execute(
                f"SELECT manifest FROM episodes {where} "
                "ORDER BY created_at, episode_uid LIMIT ? OFFSET ?",
                (*params, limit + 1, offset),
            ).fetchall()
        finally:
            conn.close()
        has_more = len(rows) > limit
        page = [json.loads(r["manifest"]) for r in rows[:limit]]
        return page, str(offset + limit) if has_more else None

    def episode_summaries(
        self,
        filters: dict[str, Any] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """One page of the fact table, newest first, filtered in SQL.

        The identity and provenance columns are promoted, so a list view reads
        them directly instead of rehydrating whole traces. Only ``metrics``
        still has to be decoded, because it is the one thing a researcher wants
        to see beside the identity and it has no fixed shape.
        """
        where, params = self._where(filters)
        offset = int(cursor) if cursor else 0
        if limit < 1:
            raise ValueError("episode limit must be at least 1")
        columns = ", ".join(_SUMMARY_COLUMNS)
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            rows = conn.execute(
                f"SELECT {columns}, metrics FROM episodes {where} "
                "ORDER BY created_at DESC, episode_uid DESC LIMIT ? OFFSET ?",
                (*params, limit + 1, offset),
            ).fetchall()
        finally:
            conn.close()
        has_more = len(rows) > limit
        page = []
        for row in rows[:limit]:
            summary: dict[str, Any] = {column: row[column] for column in _SUMMARY_COLUMNS}
            summary["stopped"] = bool(summary["stopped"])
            try:
                summary["metrics"] = json.loads(row["metrics"] or "{}")
            except json.JSONDecodeError:
                summary["metrics"] = {}
            page.append(summary)
        return page, str(offset + limit) if has_more else None

    def count_episodes(self, filters: dict[str, Any] | None = None) -> int:
        """How many rows match, without rehydrating any of them.

        The health endpoint asks this every few seconds; counting used to mean
        deserialising every trace in the corpus.
        """
        where, params = self._where(filters)
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            return int(conn.execute(
                f"SELECT COUNT(*) AS n FROM episodes {where}", params
            ).fetchone()["n"])
        finally:
            conn.close()

    def iter_episodes(
        self, filters: dict[str, Any] | None = None
    ) -> Iterator[EpisodeTrace]:
        """Stream every matching trace. Used by ``EpisodeDataset.from_store``."""
        where, params = self._where(filters)
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            for row in conn.execute(
                f"SELECT * FROM episodes {where} ORDER BY created_at, episode_uid", params
            ):
                trace = self._row_to_trace(row)
                if trace is not None:
                    yield trace
        finally:
            conn.close()

    @staticmethod
    def _where(filters: dict[str, Any] | None) -> tuple[str, tuple]:
        filters = {k: v for k, v in (filters or {}).items() if k in _FILTERABLE}
        if not filters:
            return "", ()
        clause = " AND ".join(f"{k} = ?" for k in filters)
        return f"WHERE {clause}", tuple(filters.values())

    @staticmethod
    def _row_to_trace(row: sqlite3.Row) -> EpisodeTrace | None:
        try:
            return EpisodeTrace.model_validate({
                "episode_uid": row["episode_uid"],
                "config": json.loads(row["config"]),
                "events": json.loads(row["events"]),
                "final_state": json.loads(row["final_state"]),
                "metrics": json.loads(row["metrics"]),
                "release": json.loads(row["release"] or "{}") or None,
                "episode": json.loads(row["episode"] or "{}") or None,
                "observability": json.loads(row["observability"] or "{}"),
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "stopped": bool(row["stopped"]),
            })
        except Exception as exc:
            log.warning("Skipping unreadable trace row %s: %s", row["episode_uid"], exc)
            return None

    # --- resume support ---

    def completed_episode_ids(self, experiment_name: str) -> set[str]:
        """The deterministic ids this store already holds a completed run for.

        A ``PARTIAL`` row is a trace recovered from an interrupted episode's
        event log. It is evidence, not a result, so ``--resume`` must not treat
        it as one and the progress join must not count it.
        """
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT DISTINCT episode_id FROM episodes "
                "WHERE experiment_name = ? AND status != 'PARTIAL'",
                (experiment_name,),
            ).fetchall()
        finally:
            conn.close()
        return {r["episode_id"] for r in rows if r["episode_id"]}

    # --- derived artifacts and trace-derived ratings ---

    def put_derived_artifact(self, artifact: DerivedArtifact) -> bool:
        """Persist one versioned artifact; return whether its payload changed."""
        with self._write_lock:
            conn = self._connect()
            try:
                self._ensure_schema(conn)
                existing = conn.execute(
                    "SELECT trace_digest, payload, metadata FROM derived_artifacts "
                    "WHERE episode_uid = ? AND kind = ? AND version = ?",
                    (artifact.episode_uid, artifact.kind, artifact.version),
                ).fetchone()
                encoded_payload = json.dumps(artifact.payload, sort_keys=True)
                encoded_metadata = json.dumps(artifact.metadata, sort_keys=True)
                unchanged = bool(existing) and (
                    existing["trace_digest"] == artifact.trace_digest
                    and existing["payload"] == encoded_payload
                    and existing["metadata"] == encoded_metadata
                )
                if not unchanged:
                    conn.execute(
                        "INSERT OR REPLACE INTO derived_artifacts "
                        "(episode_uid, kind, version, trace_digest, payload, metadata, created_at) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (
                            artifact.episode_uid,
                            artifact.kind,
                            artifact.version,
                            artifact.trace_digest,
                            encoded_payload,
                            encoded_metadata,
                            artifact.created_at.isoformat(),
                        ),
                    )
                    conn.commit()
                return not unchanged
            finally:
                conn.close()

    def get_derived_artifacts(self, episode_uid: str) -> list[DerivedArtifact]:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT * FROM derived_artifacts WHERE episode_uid = ? ORDER BY kind, version",
                (episode_uid,),
            ).fetchall()
        finally:
            conn.close()
        return [
            DerivedArtifact.model_validate({
                "episode_uid": row["episode_uid"],
                "kind": row["kind"],
                "version": row["version"],
                "trace_digest": row["trace_digest"],
                "payload": json.loads(row["payload"]),
                "metadata": json.loads(row["metadata"]),
                "created_at": row["created_at"],
            })
            for row in rows
        ]

    def put_rating_event(
        self, event: RatingEvent, *, adapter_version: str, trace_digest: str
    ) -> bool:
        """Persist an extracted rating event keyed by its trace and adapter version."""
        encoded = event.model_dump_json()
        with self._write_lock:
            conn = self._connect()
            try:
                self._ensure_schema(conn)
                existing = conn.execute(
                    "SELECT trace_digest, event FROM rating_events "
                    "WHERE episode_uid = ? AND adapter_version = ?",
                    (event.episode_uid, adapter_version),
                ).fetchone()
                unchanged = bool(existing) and (
                    existing["trace_digest"] == trace_digest and existing["event"] == encoded
                )
                if not unchanged:
                    conn.execute(
                        "INSERT OR REPLACE INTO rating_events "
                        "(episode_uid, environment_id, adapter_version, trace_digest, event) VALUES (?,?,?,?,?)",
                        (event.episode_uid, event.environment_id, adapter_version, trace_digest, encoded),
                    )
                    conn.commit()
                return not unchanged
            finally:
                conn.close()

    def iter_rating_events(
        self, *, environment_id: str, adapter_version: str
    ) -> Iterator[RatingEvent]:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT event FROM rating_events WHERE environment_id = ? AND adapter_version = ? "
                "ORDER BY episode_uid",
                (environment_id, adapter_version),
            ).fetchall()
        finally:
            conn.close()
        for row in rows:
            yield RatingEvent.model_validate_json(row["event"])

    def put_rating_snapshot(
        self, snapshot: RatingSnapshot, *, environment_id: str, adapter_version: str
    ) -> None:
        with self._write_lock:
            conn = self._connect()
            try:
                self._ensure_schema(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO rating_snapshots "
                    "(environment_id, adapter_version, snapshot) VALUES (?,?,?)",
                    (environment_id, adapter_version, snapshot.model_dump_json()),
                )
                conn.commit()
            finally:
                conn.close()

    def get_rating_snapshot(
        self, *, environment_id: str, adapter_version: str
    ) -> RatingSnapshot | None:
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            row = conn.execute(
                "SELECT snapshot FROM rating_snapshots WHERE environment_id = ? AND adapter_version = ?",
                (environment_id, adapter_version),
            ).fetchone()
        finally:
            conn.close()
        return RatingSnapshot.model_validate_json(row["snapshot"]) if row else None

    # --- preflight ---

    def check(self) -> StoreCheck:
        """Canary round-trip: create the schema, insert, read back, delete.

        SQLite is cheap enough to exercise for real, so this verifies the whole
        path — directory writable, file creatable, schema valid, row readable —
        rather than merely that the file exists.
        """
        started = time.monotonic()
        canary = f"__smoke__{uuid.uuid4()}"
        try:
            with self._write_lock:
                conn = self._connect()
                try:
                    self._ensure_schema(conn)
                    conn.execute(
                        "INSERT INTO episodes (episode_uid, environment_id, experiment_name,"
                        " config, events, final_state, metrics, manifest)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (canary, "__smoke__", "__smoke__", "{}", "[]", "{}", "{}", "{}"),
                    )
                    conn.commit()
                    found = conn.execute(
                        "SELECT episode_uid FROM episodes WHERE episode_uid = ?", (canary,)
                    ).fetchone()
                    conn.execute("DELETE FROM episodes WHERE episode_uid = ?", (canary,))
                    conn.commit()
                finally:
                    conn.close()
        except Exception as exc:
            return StoreCheck(
                backend=self.name, ok=False, target=str(self.path),
                detail=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.monotonic() - started) * 1000,
            )
        if found is None:
            return StoreCheck(
                backend=self.name, ok=False, target=str(self.path),
                detail="canary row did not read back after commit",
                latency_ms=(time.monotonic() - started) * 1000,
            )
        return StoreCheck(
            backend=self.name, ok=True, target=str(self.path),
            detail="write/read/delete round-trip succeeded",
            latency_ms=(time.monotonic() - started) * 1000,
        )


register_store("sqlite", SQLiteEpisodeStore)
