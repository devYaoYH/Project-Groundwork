"""Durable, append-as-you-go event log for one episode.

Events were the one part of the record that did not obey the framework's own
local-first invariant.  ``EventLog`` held them in a list and the first durable
write was ``store.put_episode()`` *after* ``environment.run()`` returned; the
only incremental copy was the optional Redis stream, published from an
in-memory queue by a daemon thread that swallows its own failures.  A runner
killed mid-episode therefore lost the whole transcript -- after the tokens were
spent -- unless ``A2A_REDIS_URL`` happened to be set.

This inverts that.  The local file is written first and always; Redis becomes a
second copy rather than the only one.

The sink is deliberately dumb -- append, flush, no rotation, no index -- because
its whole job is to survive a ``SIGKILL`` between two events.  Each line is the
same envelope ``decode_stream_events`` produces for a Redis entry, so one
projection reads either source.
"""

from __future__ import annotations

import json
import logging
import threading
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from a2a_engine.schemas import Event

log = logging.getLogger("a2a_engine.event_sink")

SINK_SUFFIX = ".events.jsonl"

#: Set by the runner around one episode so a environment's ``EventLog`` picks up its
#: sink without the sink path having to travel through the resolved config --
#: which would put a machine-specific absolute path into the research record
#: and into ``resolved_config_hash``.
current_event_sink: ContextVar["JsonlEventSink | None"] = ContextVar(
    "a2a_current_event_sink", default=None
)


class JsonlEventSink:
    """Append one JSON object per event, flushed before ``write`` returns."""

    def __init__(
        self,
        path: str | Path,
        *,
        episode_uid: str,
        episode_id: str | None = None,
        environment_id: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.episode_uid = episode_uid
        self.episode_id = episode_id
        self.environment_id = environment_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        self._lock = threading.Lock()
        self._closed = False

    def write(self, event: Event) -> None:
        """Put one event on disk. Never raises: a full disk must not lose a run."""
        line = json.dumps(
            {
                "episode_uid": self.episode_uid,
                "episode_id": self.episode_id,
                "environment_id": self.environment_id,
                "event": event.model_dump(mode="json"),
            },
            separators=(",", ":"),
            sort_keys=True,
            default=str,
        )
        with self._lock:
            if self._closed:
                return
            try:
                self._handle.write(line + "\n")
                # Flush per event, not per buffer: an unflushed Python buffer is
                # exactly what a SIGKILL discards, and that is the case this
                # file exists for.
                self._handle.flush()
            except Exception:  # pragma: no cover - defensive
                log.warning("could not append to %s", self.path, exc_info=True)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._handle.close()
            except Exception:  # pragma: no cover - defensive
                log.warning("could not close %s", self.path, exc_info=True)


def open_event_sink(
    results_dir: str | Path,
    *,
    experiment_name: str | None,
    episode_uid: str,
    episode_id: str | None = None,
    environment_id: str | None = None,
) -> JsonlEventSink | None:
    """Open the sink for one episode, or ``None`` when the path is unwritable.

    A read-only results directory degrades to no event log rather than failing
    the episode: losing recoverability is bad, losing the run is worse.
    """
    base = Path(results_dir)
    if experiment_name:
        base = base / experiment_name
    try:
        return JsonlEventSink(
            base / f"{episode_uid}{SINK_SUFFIX}",
            episode_uid=episode_uid,
            episode_id=episode_id,
            environment_id=environment_id,
        )
    except OSError:
        log.warning("event sink unavailable under %s; this episode is not recoverable", base)
        return None


def read_event_sink(path: str | Path) -> list[dict[str, Any]]:
    """Decode a sink file into stream-shaped entries.

    A process killed mid-write leaves a truncated final line.  That line is
    skipped rather than fatal -- the same tolerance ``decode_stream_events``
    already has -- because everything before it is still evidence.
    """
    entries: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict) or not isinstance(entry.get("event"), dict):
                continue
            try:
                Event.model_validate(entry["event"])
            except Exception:
                continue
            entries.append(entry)
    return entries


def iter_event_sinks(results_dir: str | Path, experiment_name: str | None = None) -> Iterator[Path]:
    """Every event log under a results tree, newest last."""
    base = Path(results_dir)
    if experiment_name:
        base = base / experiment_name
    if not base.is_dir():
        return
    yield from sorted(base.rglob(f"*{SINK_SUFFIX}"))
