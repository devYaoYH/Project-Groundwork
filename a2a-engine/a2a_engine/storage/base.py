"""TraceStore — pluggable persistence for game traces.

Mirrors the ``a2a_engine.ratings.RatingStore`` Protocol pattern already used in
this codebase: one narrow interface, several backends, chosen by config rather
than by CLI flag or hardcoded import.

The local JSON store is always the ground truth on disk. Remote backends (S3,
Firestore) are configured per game/experiment and are best-effort: a remote
failure is recorded in the manifest and never fails a run, which preserves the
behavior the calendar benchmark already relied on.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, runtime_checkable

from pydantic import BaseModel

from a2a_engine.manifest import RunManifest
from a2a_engine.schemas import GameTraceBase


class StoreCheck(BaseModel):
    """Result of a sink reachability probe. See ``TraceStore.check``."""

    backend: str
    ok: bool
    target: str | None = None
    detail: str = ""
    latency_ms: float | None = None

    def render(self) -> str:
        mark = "OK  " if self.ok else "FAIL"
        where = f" [{self.target}]" if self.target else ""
        timing = f" ({self.latency_ms:.0f}ms)" if self.latency_ms is not None else ""
        return f"{mark} {self.backend}{where}{timing}: {self.detail}"


@runtime_checkable
class TraceStore(Protocol):
    """Persistence interface for game traces."""

    name: str

    def put_trace(self, trace: GameTraceBase, manifest: RunManifest) -> str:
        """Persist a trace + its manifest. Returns a URI identifying the record."""
        ...

    def get_trace(self, game_id: str) -> GameTraceBase | None:
        """Load a single trace by game_id, or None if absent."""
        ...

    def list_traces(
        self,
        filters: dict[str, Any] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return (rows, next_cursor). Rows are manifest-shaped summary dicts."""
        ...

    def check(self) -> StoreCheck:
        """Probe the sink for reachability, without writing experiment data.

        Backends whose endpoint is local and cheap to exercise (``local``,
        ``sqlite``) do a full canary round-trip: write, read back, delete.
        Remote backends (``s3``, ``firestore``) probe read access instead, so a
        preflight never litters a shared bucket or collection.

        This is what ``a2a-run --smoke-test`` calls before running anything, so
        a misconfigured sink surfaces in seconds rather than after a batch has
        burned its budget.
        """
        ...


def check_store(store: Any) -> StoreCheck:
    """Probe a store, tolerating third-party backends with no ``check``.

    Falls back to reporting the backend as unverified rather than failing: a
    store that predates this method is not necessarily broken.
    """
    probe = getattr(store, "check", None)
    if probe is None:
        return StoreCheck(
            backend=getattr(store, "name", type(store).__name__),
            ok=True,
            detail="backend does not implement check(); reachability unverified",
        )
    started = time.monotonic()
    try:
        result = probe()
    except Exception as exc:  # a broken probe is a failed probe, not a crash
        return StoreCheck(
            backend=getattr(store, "name", type(store).__name__),
            ok=False,
            detail=f"{type(exc).__name__}: {exc}",
            latency_ms=(time.monotonic() - started) * 1000,
        )
    if result.latency_ms is None:
        result.latency_ms = (time.monotonic() - started) * 1000
    return result


def iter_traces(store: Any, *, filters: dict[str, Any] | None = None,
                limit: int | None = None) -> Iterator[GameTraceBase]:
    """Yield traces from any store by paging ``list_traces`` and hydrating each.

    Backends may override with something more direct (``SQLiteTraceStore`` reads
    rows straight out of the table); this generic path exists so that
    ``GameDataset.from_store`` works against every backend, including ones
    contributed later.
    """
    direct = getattr(store, "iter_traces", None)
    if direct is not None:
        yielded = 0
        for trace in direct(filters=filters):
            yield trace
            yielded += 1
            if limit is not None and yielded >= limit:
                return
        return

    seen = 0
    cursor: str | None = None
    while True:
        rows, cursor = store.list_traces(filters, 100, cursor)
        for row in rows:
            game_id = row.get("game_id")
            if not game_id:
                continue
            trace = store.get_trace(str(game_id))
            if trace is None:
                continue
            yield trace
            seen += 1
            if limit is not None and seen >= limit:
                return
        if not cursor:
            return


# --- backend registry -------------------------------------------------------

_BACKENDS: dict[str, Callable[..., TraceStore]] = {}


def register_store(name: str, factory: Callable[..., TraceStore]) -> None:
    """Register a TraceStore factory under a backend name."""
    _BACKENDS[name] = factory


def list_stores() -> list[str]:
    return sorted(_BACKENDS)


def make_store(spec: dict[str, Any] | None, *, results_dir: str | Path) -> TraceStore:
    """Build a TraceStore from an experiment's resolved ``storage:`` block.

        storage:
          backend: sqlite
          path: ./results/a2a.db

    The spec arriving here is already resolved — sink inheritance and ``${ENV}``
    interpolation happen in ``a2a_engine.experiment.resolve_storage``, so this
    function only maps a backend name onto its factory.

    An absent or empty spec yields the local JSON store, so a game that declares
    nothing still gets reproducible on-disk traces.
    """
    spec = dict(spec or {})
    backend = spec.pop("backend", "local")
    if backend not in _BACKENDS:
        raise KeyError(
            f"Unknown trace store backend {backend!r}. Known: {list_stores()}. "
            "Register one with a2a_engine.storage.register_store()."
        )
    spec.setdefault("results_dir", results_dir)
    return _BACKENDS[backend](**spec)


def _import_backends() -> None:
    """Import built-in backends for their registration side effects.

    Each module guards its own optional dependency (for example
    google-cloud-firestore), so importing a2a_engine.storage never
    hard-requires a cloud SDK. The legacy DynamoDB rating store separately
    declares the ``a2a-engine[aws]`` extra when it is needed.
    """
    from a2a_engine.storage import local  # noqa: F401
    from a2a_engine.storage import sqlite  # noqa: F401
    from a2a_engine.storage import s3  # noqa: F401
    from a2a_engine.storage import firestore  # noqa: F401
