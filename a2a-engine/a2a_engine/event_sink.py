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

A local file only helps a reader who can see the worker's disk.  Once the
worker is elsewhere, the same bytes also have to reach somewhere the control
plane can read, so :class:`DurableEventSink` publishes each line to the shared
artifact store as it is written and records a high-water mark when it closes.
The four functions below keep their signatures; the worker declares its
artifact target once, with :func:`configure_event_artifacts`, and every episode
it opens afterwards publishes.
"""

from __future__ import annotations

import json
import logging
import threading
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from a2a_engine.artifacts import (
    ArtifactStore,
    EventSinkArtifact,
    event_sink_key,
    watermark_key,
)
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

    def encode(self, event: Event) -> str:
        """The one on-the-wire envelope, shared by the file and the artifact."""
        return json.dumps(
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

    def write(self, event: Event) -> None:
        """Put one event on disk. Never raises: a full disk must not lose a run."""
        line = self.encode(event)
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


@dataclass(frozen=True)
class EventArtifactTarget:
    """Where this worker publishes the evidence it produces.

    Process-wide rather than a ``ContextVar`` deliberately: one worker executes
    one launch, and the episodes of that launch fan out across a thread pool
    that does not copy context.  The *sink* is per-episode and stays a
    ContextVar; the destination is not.
    """

    store: ArtifactStore
    launch_id: str


_artifact_target: EventArtifactTarget | None = None


def configure_event_artifacts(store: ArtifactStore | None, *, launch_id: str | None) -> None:
    """Declare where durable event artifacts go, or clear the declaration.

    A run with no launch identity -- ``a2a-run`` from a shell, a test, a
    notebook -- publishes nothing and keeps exactly the local-file behaviour it
    had.  Publishing is what a *dispatched* worker does, because only a
    dispatched worker has a control plane that will come looking.
    """
    global _artifact_target
    if store is None or not launch_id:
        _artifact_target = None
        return
    _artifact_target = EventArtifactTarget(store=store, launch_id=launch_id)


def event_artifact_target() -> EventArtifactTarget | None:
    return _artifact_target


class DurableEventSink(JsonlEventSink):
    """A local sink that also publishes every event to shared storage.

    Same local-first posture one layer down: the file is written and flushed
    first and unconditionally, the artifact is published after, and a failed
    publish is logged rather than raised -- losing recoverability is bad,
    losing the run is worse.  What the high-water mark adds is that the
    degradation becomes *visible*: a ``PARTIAL`` trace says how far durability
    reached instead of presenting a truncated event list as the whole story.
    """

    def __init__(self, *args: Any, store: ArtifactStore, launch_id: str, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._store = store
        self._launch_id = launch_id
        self._durable_through = 0
        self._watermark_written = False
        self._key = event_sink_key(launch_id, self.episode_id or "", self.episode_uid)
        self._watermark_key = watermark_key(launch_id, self.episode_id or "", self.episode_uid)

    @property
    def durable_through(self) -> int:
        """How many events reached the artifact store."""
        return self._durable_through

    @property
    def artifact(self) -> EventSinkArtifact:
        """This episode's stream as a reader would address it."""
        return EventSinkArtifact(
            launch_id=self._launch_id,
            episode_id=self.episode_id or "",
            episode_uid=self.episode_uid,
            uri=self._store.uri(self._key),
            store=self._store,
            watermark=self._durable_through,
            closed=self._watermark_written,
        )

    def write(self, event: Event) -> None:
        super().write(event)
        line = self.encode(event)
        with self._lock:
            # A write after close is dropped by the file copy, so it must be
            # dropped here too: an artifact holding an event the sink says it
            # never wrote would make the watermark a lie.
            if self._closed:
                return
            try:
                self._store.append(self._key, (line + "\n").encode("utf-8"))
            except Exception:
                log.warning("could not publish an event to %s", self._key, exc_info=True)
                return
            self._durable_through += 1

    def close(self) -> None:
        self._publish_watermark()
        super().close()

    def _publish_watermark(self) -> None:
        with self._lock:
            if self._closed or self._watermark_written:
                return
            durable_through = self._durable_through
        try:
            self._store.put(
                self._watermark_key,
                json.dumps(
                    {"durable_through": durable_through, "closed": True},
                    separators=(",", ":"), sort_keys=True,
                ).encode("utf-8"),
            )
        except Exception:
            log.warning("could not publish the watermark for %s", self._key, exc_info=True)
            return
        with self._lock:
            self._watermark_written = True


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
    path = base / f"{episode_uid}{SINK_SUFFIX}"
    target = event_artifact_target()
    try:
        # Without an episode id there is nothing the control plane could match
        # a published artifact back to, so publishing it would be evidence
        # nobody can find. The local file is still written.
        if target is None or not episode_id:
            return JsonlEventSink(
                path,
                episode_uid=episode_uid,
                episode_id=episode_id,
                environment_id=environment_id,
            )
        return DurableEventSink(
            path,
            episode_uid=episode_uid,
            episode_id=episode_id,
            environment_id=environment_id,
            store=target.store,
            launch_id=target.launch_id,
        )
    except OSError:
        log.warning("event sink unavailable under %s; this episode is not recoverable", base)
        return None


def read_event_sink(source: "str | Path | EventSinkArtifact") -> list[dict[str, Any]]:
    """Decode a sink into stream-shaped entries, from a file or an artifact.

    A process killed mid-write leaves a truncated final line.  That line is
    skipped rather than fatal -- the same tolerance ``decode_stream_events``
    already has -- because everything before it is still evidence.

    The artifact form is what recovery uses once the worker's filesystem is not
    the reader's: same decoding, same tolerance, different transport.
    """
    if isinstance(source, EventSinkArtifact):
        text = source.read_bytes().decode("utf-8", errors="replace")
    else:
        text = Path(source).read_text(encoding="utf-8", errors="replace")
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
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
