"""SQLite trace store — the lightweight "local DB" sink.

This is the zero-setup option: no bucket, no GCP project, no credentials. A new
contributor points ``storage.path`` at a file and gets a queryable corpus of
runs they can open with any SQLite client.

Unlike :mod:`a2a_engine.storage.s3` and :mod:`a2a_engine.storage.firestore`,
which mirror a local JSON tree to a remote and treat the remote as best-effort,
SQLite *is* the ground truth when selected. Writing both a JSON tree and a
database would double the on-disk footprint and leave two records that can
disagree. Set ``mirror_json: true`` if you want the JSON tree as well.

Schema (one row per run; ``events`` is the trace's event array as JSON):

    episodes(episode_uid PK, environment_id, experiment_name, episode_id,
           cell_id, episode_idx, config, events, final_state, metrics, release, episode,
           started_at, ended_at, stopped, manifest, created_at)

``episode_id`` is indexed rather than unique: re-running an experiment
after a crash legitimately produces a second attempt at the same run id, and
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
from a2a_engine.ratings.schemas import RatingEvent, RatingSnapshot
from a2a_engine.schemas import EpisodeTrace
from a2a_engine.storage.base import StoreCheck, register_store
from a2a_engine.storage.local import LocalJSONStore

log = logging.getLogger("a2a_engine.storage.sqlite")

DEFAULT_DB_NAME = "a2a_traces.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    episode_uid           TEXT PRIMARY KEY,
    environment_id         TEXT,
    experiment_name   TEXT,
    episode_id TEXT,
    cell_id       TEXT,
    episode_idx           INTEGER,
    config            TEXT NOT NULL,
    events            TEXT NOT NULL,
    final_state       TEXT NOT NULL,
    metrics           TEXT NOT NULL,
    release       TEXT NOT NULL DEFAULT '{}',
    episode           TEXT NOT NULL DEFAULT '{}',
    observability     TEXT NOT NULL DEFAULT '{}',
    started_at        TEXT,
    ended_at          TEXT,
    stopped           INTEGER DEFAULT 0,
    manifest          TEXT NOT NULL,
    created_at        TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_episodes_experiment ON episodes(experiment_name);
CREATE INDEX IF NOT EXISTS idx_episodes_environment ON episodes(environment_id);
CREATE INDEX IF NOT EXISTS idx_episodes_episode_id ON episodes(episode_id);

-- Derived data is intentionally separate from the immutable environment trace. Both
-- tables are keyed by environment ID plus extractor version, so a new analysis can be
-- backfilled without mutating the source record or double-counting a result.
CREATE TABLE IF NOT EXISTS derived_artifacts (
    episode_uid      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    version      TEXT NOT NULL,
    trace_digest TEXT NOT NULL,
    payload      TEXT NOT NULL,
    metadata     TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (episode_uid, kind, version)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_episode ON derived_artifacts(episode_uid);

CREATE TABLE IF NOT EXISTS rating_events (
    episode_uid         TEXT NOT NULL,
    environment_id       TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    trace_digest    TEXT NOT NULL,
    event           TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (episode_uid, adapter_version)
);
CREATE INDEX IF NOT EXISTS idx_rating_events_environment
    ON rating_events(environment_id, adapter_version);

CREATE TABLE IF NOT EXISTS rating_snapshots (
    environment_id       TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    snapshot        TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (environment_id, adapter_version)
);
"""

# Columns a caller may filter list_episodes() on. Restricting to real columns
# keeps the filter clause parameterised and prevents a filter key from being
# interpolated into SQL.
_FILTERABLE = {
    "episode_uid", "environment_id", "experiment_name", "episode_id",
    "cell_id", "episode_idx", "stopped",
}


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
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        if self._initialised:
            return
        conn.executescript(_SCHEMA)
        # SQLite's CREATE TABLE IF NOT EXISTS does not migrate a pre-existing
        # local corpus. Keep this migration additive so old trace DBs remain
        # readable after observability provenance was introduced.
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(episodes)")}
        if "observability" not in columns:
            conn.execute(
                "ALTER TABLE episodes ADD COLUMN observability TEXT NOT NULL DEFAULT '{}'"
            )
        if "release" not in columns:
            conn.execute(
                "ALTER TABLE episodes ADD COLUMN release TEXT NOT NULL DEFAULT '{}'"
            )
        if "episode" not in columns:
            conn.execute(
                "ALTER TABLE episodes ADD COLUMN episode TEXT NOT NULL DEFAULT '{}'"
            )
        conn.commit()
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
        row = (
            trace.episode_uid,
            manifest.environment_id or payload.get("config", {}).get("environment_id"),
            manifest.experiment_name,
            manifest.episode_id,
            manifest.cell_id,
            manifest.episode_idx,
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
                    # The manifest is written last and includes the status we are
                    # about to commit, so serialise it after the row is staged.
                    manifest.storage.uri = self.uri(trace.episode_uid)
                    manifest.storage.status = "written"
                    conn.execute(
                        "INSERT OR REPLACE INTO episodes ("
                        "  episode_uid, environment_id, experiment_name, episode_id,"
                        "  cell_id, episode_idx, config, events, final_state, metrics, release, episode,"
                        "  observability, started_at, ended_at, stopped, manifest"
                        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            rows = conn.execute(
                "SELECT DISTINCT episode_id FROM episodes WHERE experiment_name = ?",
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
