"""Local JSON trace store — the on-disk ground truth.

Layout is unchanged from the pre-merge framework so existing tooling and the
calendar analysis scripts keep working:

    <results_dir>/<experiment_name>/<episode_uid>.json
    <results_dir>/<experiment_name>/<episode_uid>.manifest.json
    <results_dir>/<experiment_name>/_run_manifest.jsonl

``.metadata.json`` is still written as a compatibility alias for the manifest,
because ``expt_runner``'s resume logic and several calendar scripts glob for it.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from a2a_engine.manifest import EpisodeManifest
from a2a_engine.schemas import EpisodeTrace
from a2a_engine.storage.base import StoreCheck, register_store
from a2a_engine.tracing import write_episode

_manifest_lock = threading.Lock()


class LocalJSONStore:
    """Writes episodes and manifests to the local filesystem."""

    name = "local"

    def __init__(self, results_dir: str | Path = "./results", **_ignored: Any) -> None:
        self.results_dir = Path(results_dir)

    # --- write ---

    def put_episode(self, trace: EpisodeTrace, manifest: EpisodeManifest) -> str:
        trace_path = write_episode(
            trace, self.results_dir, experiment_name=manifest.experiment_name
        )
        manifest.local_trace_path = str(trace_path)
        manifest.trace_size_bytes = trace_path.stat().st_size
        # Remote backends overwrite these after their own upload attempt; when
        # local *is* the backend, the write is already final here.
        if manifest.storage.backend == "local":
            manifest.storage.uri = str(trace_path)
            manifest.storage.status = "written"

        self.write_manifest(manifest, trace_path)
        return str(trace_path)

    def write_manifest(self, manifest: EpisodeManifest, trace_path: Path) -> Path:
        """Write the sidecar manifest and append to the experiment's JSONL index."""
        blob = manifest.model_dump_json(indent=2)
        manifest_path = trace_path.with_name(f"{trace_path.stem}.manifest.json")
        manifest_path.write_text(blob + "\n")
        # Compatibility alias: pre-merge tooling globs for *.metadata.json.
        trace_path.with_name(f"{trace_path.stem}.metadata.json").write_text(blob + "\n")
        self._append_index(manifest)
        return manifest_path

    def _append_index(self, manifest: EpisodeManifest) -> None:
        path = self.results_dir / manifest.experiment_name / "_run_manifest.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with _manifest_lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(manifest.model_dump_json())
                f.write("\n")

    # --- read ---

    def get_episode(self, episode_uid: str) -> EpisodeTrace | None:
        for path in self.results_dir.rglob(f"{episode_uid}.json"):
            return EpisodeTrace.model_validate_json(path.read_text())
        return None

    def list_episodes(
        self,
        filters: dict[str, Any] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        filters = filters or {}
        rows: list[dict[str, Any]] = []
        for path in sorted(self.results_dir.rglob("*.manifest.json")):
            try:
                row = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if all(row.get(k) == v for k, v in filters.items()):
                rows.append(row)
        start = int(cursor) if cursor else 0
        page = rows[start : start + limit]
        next_cursor = str(start + limit) if start + limit < len(rows) else None
        return page, next_cursor

    # --- preflight ---

    def check(self) -> StoreCheck:
        """Verify the results directory is creatable and writable."""
        started = time.monotonic()
        canary = self.results_dir / f".__smoke__{uuid.uuid4()}"
        try:
            self.results_dir.mkdir(parents=True, exist_ok=True)
            canary.write_text("ok")
            read_back = canary.read_text()
            canary.unlink()
        except Exception as exc:
            return StoreCheck(
                backend=self.name, ok=False, target=str(self.results_dir),
                detail=f"{type(exc).__name__}: {exc}",
                latency_ms=(time.monotonic() - started) * 1000,
            )
        return StoreCheck(
            backend=self.name, ok=read_back == "ok", target=str(self.results_dir),
            detail="results directory is writable",
            latency_ms=(time.monotonic() - started) * 1000,
        )

    # --- resume support ---

    def completed_episode_ids(self, experiment_name: str) -> set[str]:
        """Run ids already persisted, for ``--resume``.

        Reads the JSONL index first, then falls back to scanning sidecars and
        episodes, so resume still works against results produced before the
        manifest existed.
        """
        base = self.results_dir / experiment_name
        completed: set[str] = set()

        index = base / "_run_manifest.jsonl"
        if index.exists():
            for line in index.read_text().splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                _add_if_present(completed, record)

        if not base.exists():
            return completed

        for pattern in ("*.manifest.json", "*.metadata.json"):
            for path in base.glob(pattern):
                try:
                    _add_if_present(completed, json.loads(path.read_text()))
                except (json.JSONDecodeError, OSError):
                    continue

        for path in base.glob("*.json"):
            if path.name.endswith((".manifest.json", ".metadata.json")):
                continue
            try:
                trace = EpisodeTrace.model_validate_json(path.read_text())
            except Exception:
                continue
            if trace.config.episode_id:
                completed.add(str(trace.config.episode_id))
        return completed


def _add_if_present(completed: set[str], record: dict) -> None:
    """Count a run as complete only if its trace file still exists."""
    run_id = record.get("episode_id")
    trace_path = record.get("local_trace_path")
    if run_id and (not trace_path or Path(trace_path).exists()):
        completed.add(str(run_id))


register_store("local", LocalJSONStore)
