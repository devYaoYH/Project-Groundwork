"""Document metadata and private immutable artifacts for local and GCP runtimes.

These stores deliberately contain no user authorization policy: the API checks
ownership and the trusted worker adds ownership from the admitted launch.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from contextlib import contextmanager
from typing import Any

TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED", "DRY_RUN"}
ACTIVE = {"QUEUED", "DISPATCHING", "DISPATCHED", "RUNNING", "CANCELLING"}


def _identifier(value: str) -> str:
    if not value or "/" in value or value in {".", ".."}:
        raise ValueError("invalid document identifier")
    return value


class Repository:
    def __init__(self, *, backend: str | None = None, data_dir: str | Path | None = None,
                 project: str | None = None, client=None):
        self.backend = backend or os.getenv("PLATFORM_BACKEND", "local")
        self.data_dir = Path(data_dir or os.getenv("PLATFORM_DATA_DIR", "/data/platform"))
        if self.backend == "gcp":
            from google.cloud import firestore
            self.client = client or firestore.Client(project=project or os.getenv("GOOGLE_CLOUD_PROJECT"))
        elif self.backend == "local":
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.database = self.data_dir / "metadata.sqlite"
            with self._db() as db:
                db.execute("CREATE TABLE IF NOT EXISTS documents (kind TEXT, id TEXT, data TEXT NOT NULL, PRIMARY KEY(kind,id))")
        else:
            raise ValueError("PLATFORM_BACKEND must be local or gcp")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _ref(self, kind, identifier):
        return self.client.collection("groundwork_" + _identifier(kind)).document(_identifier(identifier))

    def get(self, kind: str, identifier: str) -> dict[str, Any] | None:
        _identifier(kind); _identifier(identifier)
        if self.backend == "gcp":
            return self._ref(kind, identifier).get().to_dict()
        with self._db() as db:
            row = db.execute("SELECT data FROM documents WHERE kind=? AND id=?", (kind, identifier)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, kind: str, identifier: str, data: dict) -> None:
        _identifier(kind); _identifier(identifier)
        if self.backend == "gcp":
            self._ref(kind, identifier).set(data)
            return
        with self._db() as db:
            db.execute("INSERT INTO documents VALUES (?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data", (kind, identifier, json.dumps(data)))

    def create(self, kind: str, identifier: str, data: dict) -> bool:
        _identifier(kind); _identifier(identifier)
        if self.backend == "gcp":
            from google.api_core.exceptions import AlreadyExists
            try:
                self._ref(kind, identifier).create(data)
                return True
            except AlreadyExists:
                return False
        with self._db() as db:
            return db.execute("INSERT OR IGNORE INTO documents VALUES (?,?,?)", (kind, identifier, json.dumps(data))).rowcount == 1

    def next_attempt(self, experiment_id: str) -> int:
        """Reserve a monotonic attempt ordinal without reusing rejected plans.

        Gaps are acceptable; reusing an ordinal after a launch was compiled
        would make otherwise distinct scientific evidence ambiguous.
        """
        _identifier(experiment_id)
        if self.backend == "gcp":
            from google.cloud import firestore
            @firestore.transactional
            def commit(transaction):
                ref = self._ref("experiments", experiment_id)
                record = ref.get(transaction=transaction).to_dict()
                if record is None:
                    raise KeyError("experiment not found")
                ordinal = int(record.get("launch_ordinal", 0)) + 1
                transaction.update(ref, {"launch_ordinal": ordinal})
                return ordinal
            return commit(self.client.transaction())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM documents WHERE kind='experiments' AND id=?", (experiment_id,)).fetchone()
            if row is None:
                raise KeyError("experiment not found")
            record = json.loads(row[0])
            ordinal = int(record.get("launch_ordinal", 0)) + 1
            record["launch_ordinal"] = ordinal
            db.execute("UPDATE documents SET data=? WHERE kind='experiments' AND id=?", (json.dumps(record), experiment_id))
            return ordinal

    def list(self, kind: str, owner_id: str | None = None, experiment_id: str | None = None) -> list[dict]:
        _identifier(kind)
        if self.backend == "gcp":
            from google.cloud.firestore_v1.base_query import FieldFilter
            query = self.client.collection("groundwork_" + kind)
            # Use one indexed equality; applying the second filter in memory
            # avoids requiring a composite index just to start the platform.
            if owner_id is not None:
                query = query.where(filter=FieldFilter("owner_id", "==", owner_id))
            elif experiment_id is not None:
                query = query.where(filter=FieldFilter("experiment_id", "==", experiment_id))
            records = [snapshot.to_dict() for snapshot in query.stream()]
        else:
            with self._db() as db:
                records = [json.loads(row[0]) for row in db.execute("SELECT data FROM documents WHERE kind=? ORDER BY id", (kind,))]
        return [r for r in records if (owner_id is None or r.get("owner_id") == owner_id)
                and (experiment_id is None or r.get("experiment_id") == experiment_id)]

    def claim(self, kind: str, identifier: str, expected_statuses, patch: dict, *, expected_fields: dict | None = None) -> dict | None:
        """Compare and update a document atomically; duplicate deliveries lose."""
        _identifier(kind); _identifier(identifier)
        def update(data, read_quota, write_quota):
            if not data or data.get("status") not in expected_statuses:
                return None
            if expected_fields and any(data.get(key) != value for key, value in expected_fields.items()):
                return None
            result = {**data, **patch}
            if kind == "launches" and data.get("_admitted") and data.get("status") not in TERMINAL and result.get("status") in TERMINAL:
                quota = read_quota(data["owner_id"]) or {"active": 0}
                write_quota(data["owner_id"], {**quota, "active": max(0, quota.get("active", 0) - 1)})
            return result

        if self.backend == "gcp":
            from google.cloud import firestore
            @firestore.transactional
            def commit(transaction):
                ref = self._ref(kind, identifier)
                data = ref.get(transaction=transaction).to_dict()
                writes = []
                result = update(data, lambda owner: self._ref("quotas", owner).get(transaction=transaction).to_dict(), lambda owner, quota: writes.append((self._ref("quotas", owner), quota)))
                for quota_ref, quota in writes:
                    transaction.set(quota_ref, quota)
                if result is not None:
                    transaction.set(ref, result)
                return result
            return commit(self.client.transaction())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            def read_quota(owner):
                row = db.execute("SELECT data FROM documents WHERE kind='quotas' AND id=?", (owner,)).fetchone()
                return json.loads(row[0]) if row else None
            def write_quota(owner, quota):
                db.execute("INSERT INTO documents VALUES ('quotas',?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data", (owner, json.dumps(quota)))
            row = db.execute("SELECT data FROM documents WHERE kind=? AND id=?", (kind, identifier)).fetchone()
            result = update(json.loads(row[0]) if row else None, read_quota, write_quota)
            if result is not None:
                db.execute("UPDATE documents SET data=? WHERE kind=? AND id=?", (json.dumps(result), kind, identifier))
            return result

    def reconcile_stale(self, now: datetime | str | None = None, *, threshold_seconds: int = 180,
                        boot_timeout_seconds: int = 900, owner_id: str | None = None,
                        launch_id: str | None = None) -> list[dict]:
        """Expire a dead worker's claim without automatically repeating spend.

        Cloud event objects and immutable plans remain untouched. A concurrent
        heartbeat wins against reconciliation even after the list was read.
        """
        if threshold_seconds < 60:
            raise ValueError("stale threshold must be at least 60 seconds")
        if boot_timeout_seconds < 60:
            raise ValueError("boot timeout must be at least 60 seconds")
        current = datetime.fromisoformat(now) if isinstance(now, str) else now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        cutoff = current - timedelta(seconds=threshold_seconds)
        records = [self.get("launches", launch_id)] if launch_id else self.list("launches", owner_id=owner_id)
        interrupted = []
        for record in records:
            if not record or record.get("status") not in ACTIVE or (owner_id is not None and record.get("owner_id") != owner_id):
                continue
            boot_pending = record["status"] in {"QUEUED", "DISPATCHING", "DISPATCHED"}
            timestamp = record.get("created_at") if boot_pending else record.get("heartbeat_at") or record.get("started_at")
            if not timestamp:
                continue
            try:
                heartbeat = datetime.fromisoformat(timestamp)
                if heartbeat.tzinfo is None:
                    heartbeat = heartbeat.replace(tzinfo=UTC)
            except (TypeError, ValueError):
                continue
            deadline = current - timedelta(seconds=boot_timeout_seconds) if boot_pending else cutoff
            if heartbeat >= deadline:
                continue
            expected = {key: record.get(key) for key in ("heartbeat_at", "started_at", "created_at", "execution_name")}
            reason = "Worker did not start within its admission deadline" if boot_pending else "Worker heartbeat expired"
            result = self.claim("launches", record["id"], {record["status"]}, {
                "status": "INTERRUPTED", "ended_at": current.isoformat(),
                "error": reason + "; evidence preserved, explicit retry required",
                "evidence_complete": False}, expected_fields=expected)
            if result:
                interrupted.append(result)
                for attempt in self.list("attempts", owner_id=result["owner_id"], experiment_id=result["experiment_id"]):
                    if attempt.get("launch_id") == result["id"]:
                        self.claim("attempts", attempt["id"], {"QUEUED", "RUNNING"}, {
                            "status": "INTERRUPTED", "ended_at": current.isoformat(), "error": result["error"]})
        return interrupted

    def admit_launch(self, data: dict, *, max_active: int = 2, max_episodes: int = 100) -> dict:
        """Reserve per-owner capacity and create the launch in one transaction."""
        if int(data.get("episode_count", 0)) > max_episodes:
            raise ValueError(f"launch limit is {max_episodes} episodes")
        identifier = _identifier(data["id"]); owner = _identifier(data["owner_id"])
        record = {**data, "status": "QUEUED", "_admitted": True}
        if self.backend == "gcp":
            from google.cloud import firestore
            @firestore.transactional
            def commit(transaction):
                launch_ref = self._ref("launches", identifier); quota_ref = self._ref("quotas", owner)
                existing = launch_ref.get(transaction=transaction).to_dict()
                quota = quota_ref.get(transaction=transaction).to_dict() or {"active": 0}
                if existing:
                    return existing
                if quota.get("active", 0) >= max_active:
                    raise ValueError("concurrent launch limit reached")
                if data.get("retry_incomplete"):
                    from google.cloud.firestore_v1.base_query import FieldFilter
                    launches = self.client.collection("groundwork_launches").where(
                        filter=FieldFilter("owner_id", "==", owner)).stream(transaction=transaction)
                    if any(row.to_dict().get("experiment_id") == data["experiment_id"] and
                           row.to_dict().get("status") in ACTIVE for row in launches):
                        raise ValueError("Wait for active experiment launches to finish before retrying")
                transaction.create(launch_ref, record)
                transaction.set(quota_ref, {**quota, "active": quota.get("active", 0) + 1})
                return record
            return commit(self.client.transaction())
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT data FROM documents WHERE kind='launches' AND id=?", (identifier,)).fetchone()
            if existing:
                return json.loads(existing[0])
            row = db.execute("SELECT data FROM documents WHERE kind='quotas' AND id=?", (owner,)).fetchone()
            quota = json.loads(row[0]) if row else {"active": 0}
            if quota.get("active", 0) >= max_active:
                raise ValueError("concurrent launch limit reached")
            if data.get("retry_incomplete"):
                launches = (json.loads(row[0]) for row in db.execute(
                    "SELECT data FROM documents WHERE kind='launches'"))
                if any(row.get("owner_id") == owner and row.get("experiment_id") == data["experiment_id"]
                       and row.get("status") in ACTIVE for row in launches):
                    raise ValueError("Wait for active experiment launches to finish before retrying")
            db.execute("INSERT INTO documents VALUES ('launches',?,?)", (identifier, json.dumps(record)))
            db.execute("INSERT INTO documents VALUES ('quotas',?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data", (owner, json.dumps({**quota, "active": quota.get("active", 0) + 1})))
        return record


class ArtifactStore:
    def __init__(self, *, backend: str | None = None, data_dir: str | Path | None = None,
                 bucket: str | None = None, project: str | None = None, client=None):
        self.backend = backend or os.getenv("PLATFORM_BACKEND", "local")
        self.root = Path(data_dir or os.getenv("PLATFORM_DATA_DIR", "/data/platform")) / "artifacts"
        if self.backend == "gcp":
            from google.cloud import storage
            client = client or storage.Client(project=project or os.getenv("GOOGLE_CLOUD_PROJECT"))
            self.bucket = client.bucket(bucket or os.environ["PLATFORM_BUCKET"])
        elif self.backend == "local":
            self.root.mkdir(parents=True, exist_ok=True)
        else:
            raise ValueError("PLATFORM_BACKEND must be local or gcp")

    @staticmethod
    def _key(key):
        path = PurePosixPath(key)
        if not key or path.is_absolute() or ".." in path.parts or "\\" in key:
            raise ValueError("invalid artifact key")
        return str(path)

    def put(self, key: str, data: bytes, immutable: bool = True, *, timeout: float | None = None) -> str:
        key = self._key(key)
        if self.backend == "gcp":
            from google.api_core.exceptions import PreconditionFailed
            blob = self.bucket.blob(key)
            blob.metadata = {"sha256": hashlib.sha256(data).hexdigest()}
            started = time.monotonic()
            options = {"timeout": timeout, "retry": None} if timeout is not None else {}
            try:
                blob.upload_from_string(data, if_generation_match=0 if immutable else None, **options)
            except PreconditionFailed:
                remaining = timeout - (time.monotonic() - started) if timeout is not None else None
                if remaining is not None and remaining <= 0:
                    raise TimeoutError("immutable artifact verification exceeded its deadline") from None
                if self.get(key, timeout=remaining) != data:
                    raise ValueError(f"immutable artifact conflict: {key}") from None
            return key
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        if immutable:
            # Link an already flushed temporary file atomically so readers can
            # never observe a partially uploaded artifact.
            import tempfile
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".upload-", delete=False) as handle:
                temp = Path(handle.name)
                handle.write(data); handle.flush(); os.fsync(handle.fileno())
            try:
                try:
                    os.link(temp, path)
                except FileExistsError:
                    if path.read_bytes() != data:
                        raise ValueError(f"immutable artifact conflict: {key}") from None
            finally:
                temp.unlink(missing_ok=True)
        else:
            import tempfile
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".upload-", delete=False) as handle:
                temp = Path(handle.name); handle.write(data); handle.flush(); os.fsync(handle.fileno())
            os.replace(temp, path)
        return key

    def get(self, key: str, *, timeout: float | None = None) -> bytes:
        key = self._key(key)
        if self.backend == "gcp":
            options = {"timeout": timeout, "retry": None} if timeout is not None else {}
            return self.bucket.blob(key).download_as_bytes(**options)
        return (self.root / key).read_bytes()

    def size(self, key: str) -> int:
        key = self._key(key)
        if self.backend == "gcp":
            blob = self.bucket.blob(key)
            blob.reload()
            return int(blob.size)
        return (self.root / key).stat().st_size

    def keys(self, prefix: str = "") -> list[str]:
        if prefix:
            self._key(prefix)
        if self.backend == "gcp":
            return sorted(blob.name for blob in self.bucket.list_blobs(prefix=prefix))
        return sorted(str(path.relative_to(self.root)) for path in self.root.rglob("*") if path.is_file() and not any(part.startswith(".upload-") for part in path.relative_to(self.root).parts) and str(path.relative_to(self.root)).startswith(prefix))


_event_store: ArtifactStore | None = None
_event_lock = threading.Lock()


def durable_event_write(envelope: dict, sequence: int) -> None:
    """Fail-closed synchronous event sink called before a cloud turn returns."""
    global _event_store
    with _event_lock:
        if _event_store is None:
            _event_store = ArtifactStore()
    prefix = os.environ["A2A_DURABLE_EVENT_PREFIX"]
    uid = _identifier(envelope["episode_uid"])
    _event_store.put(f"{prefix}/{uid}/{sequence:08d}.json", json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode())
