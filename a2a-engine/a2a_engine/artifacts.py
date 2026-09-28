"""Addressable, digest-bound artifacts shared across the execution boundary.

The control plane and the worker no longer get to assume they share a
filesystem.  Everything that has to cross between them -- the launch plan now,
the durable event stream next -- crosses as an *artifact*: bytes published
under a key, addressed by a URI, and verified against a SHA-256 digest before
anything parses them.

Two deliberate postures meet here.  Publishing follows the local-first
durability pattern the rest of the codebase uses: write the bytes, return the
URI, no retries invented.  *Reading* follows the identity posture instead --
:func:`fetch_verified` raises :class:`ArtifactDigestMismatch` rather than
degrading, because a plan whose bytes changed after publication is not a
degraded plan, it is a different one.

Backends register by name exactly as ``a2a_engine.storage.make_store`` does, so
a bucket-backed store later is an additive import rather than an ``if`` branch.
A backend also registers the URI schemes it can read, which is what lets a
worker resolve a reference without being told which store produced it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple, Protocol, runtime_checkable
from urllib.parse import quote, unquote, urlparse

log = logging.getLogger("a2a_engine.artifacts")


class ArtifactDigestMismatch(ValueError):
    """Published bytes and the digest they were published under disagree.

    Raised rather than logged. The observability path degrades to empty; the
    identity path refuses to run.
    """


class ArtifactRef(NamedTuple):
    """Where an artifact lives and what it must hash to."""

    uri: str
    sha256: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@runtime_checkable
class ArtifactStore(Protocol):
    """Publish and resolve immutable objects by key."""

    name: str

    def uri(self, key: str) -> str:
        """The address ``key`` would be published under, without publishing it."""
        ...

    def put(self, key: str, data: bytes) -> ArtifactRef:
        """Publish ``data`` under ``key`` and return its reference."""
        ...

    def append(self, key: str, data: bytes) -> str:
        """Durably add ``data`` to the object at ``key``, returning its URI.

        Separate from :meth:`put` because an event stream is published one
        event at a time and re-publishing the whole object per event is
        quadratic.  A backend with no native append implements this as
        read-modify-write; a local directory implements it as an append and an
        ``fsync``.
        """
        ...

    def get(self, uri: str) -> bytes:
        """Read back bytes previously published by this store."""
        ...

    def iter_keys(self, prefix: str = "") -> Iterator[str]:
        """Every key this store holds under ``prefix``.

        Enumeration is on the protocol because recovery is enumeration: the
        control plane finds a crashed worker's evidence by listing a prefix,
        which is the one thing every object store can do and no shared
        filesystem is required for.
        """
        ...


class LocalArtifactStore:
    """A directory tree of published objects.

    Keys are ``/``-separated and map onto a relative path under ``root``. This
    is the one backend Phase 1 needs, and it is still a real artifact store
    from the worker's point of view: the worker resolves a URI and verifies a
    digest rather than reaching for a path it was handed.
    """

    name = "local"

    def __init__(self, root: str | Path, **_ignored: Any) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        candidate = (self.root / key.strip("/")).resolve()
        if self.root not in candidate.parents:
            raise ValueError(f"artifact key {key!r} escapes the artifact root")
        return candidate

    def uri(self, key: str) -> str:
        return self._path(key).as_uri()

    def put(self, key: str, data: bytes) -> ArtifactRef:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a reader never observes a half-written object,
        # and so a failed publish leaves no artifact rather than a truncated
        # one that would fail its digest check far from the cause.
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            staged = Path(handle.name)
        staged.replace(path)
        return ArtifactRef(uri=path.as_uri(), sha256=sha256_bytes(data))

    def append(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as handle:
            handle.write(data)
            # Flushed, not fsynced -- exactly the posture ``JsonlEventSink``
            # takes one layer up. The case this exists for is a killed worker
            # process, and an unflushed Python buffer is what a SIGKILL
            # discards; paying an fsync per event would buy durability against
            # a power loss at a cost the per-event publish cannot afford.
            handle.flush()
        return path.as_uri()

    def get(self, uri: str) -> bytes:
        return read_file_uri(uri)

    def iter_keys(self, prefix: str = "") -> Iterator[str]:
        base = self._path(prefix) if prefix else self.root
        if not base.is_dir():
            return
        for path in sorted(base.rglob("*")):
            if path.is_file():
                yield path.relative_to(self.root).as_posix()

    def iter_event_sinks(
        self, launch_id: str, *, episode_id: str | None = None
    ) -> Iterator["EventSinkArtifact"]:
        return iter_event_sinks(self, launch_id, episode_id=episode_id)


# --- the event-artifact key layout ------------------------------------------
#
#   <artifact root>/
#   └── launches/<launch_id>/
#       ├── plan.yaml                                   # the launch input
#       └── episodes/<episode_id>/<episode_uid>/
#           ├── events.jsonl                            # published per event
#           └── watermark.json                          # {"durable_through", "closed"}
#
# This is what replaces "glob the worker's results directory".  The control
# plane enumerates a prefix it owns rather than a filesystem the worker owns,
# which is the whole difference between recovery that works locally and
# recovery that works at all.

LAUNCHES_PREFIX = "launches"
EVENTS_OBJECT = "events.jsonl"
WATERMARK_OBJECT = "watermark.json"


def _segment(value: str) -> str:
    """One key segment, safe for a path and reversible.

    ``episode_id`` is ``"<experiment name>.<cell_id>.<idx>"`` and an experiment
    is named by a researcher, so it can hold spaces and, in principle, a
    separator.  Percent-encoding keeps a key one segment deep without inventing
    a second identifier nothing else in the record uses.
    """
    return quote(value, safe="")


def plan_key(launch_id: str) -> str:
    return f"{LAUNCHES_PREFIX}/{_segment(launch_id)}/plan.yaml"


def episode_prefix(launch_id: str, episode_id: str | None = None,
                   episode_uid: str | None = None) -> str:
    key = f"{LAUNCHES_PREFIX}/{_segment(launch_id)}/episodes"
    if episode_id is not None:
        key += f"/{_segment(episode_id)}"
        if episode_uid is not None:
            key += f"/{_segment(episode_uid)}"
    return key


def event_sink_key(launch_id: str, episode_id: str, episode_uid: str) -> str:
    return f"{episode_prefix(launch_id, episode_id, episode_uid)}/{EVENTS_OBJECT}"


def watermark_key(launch_id: str, episode_id: str, episode_uid: str) -> str:
    return f"{episode_prefix(launch_id, episode_id, episode_uid)}/{WATERMARK_OBJECT}"


@dataclass(frozen=True)
class EventSinkArtifact:
    """One episode's durable event stream, as the control plane sees it.

    ``watermark`` is how far durability actually reached, recorded by the sink
    when it closed.  ``None`` means the sink never got to say -- which is
    precisely the SIGKILL case -- and the honest reading is then the number of
    entries the artifact holds, because an event that is in the artifact is an
    event that was durable.
    """

    launch_id: str
    episode_id: str
    episode_uid: str
    uri: str
    store: Any = field(repr=False, default=None)
    watermark: int | None = None
    closed: bool = False

    def read_bytes(self) -> bytes:
        return self.store.get(self.uri)

    def observed_through(self) -> int:
        """How far durability has reached *right now*, closed or not.

        ``watermark`` only exists once the sink closed, so a live episode has
        none -- and a live episode is precisely the one a researcher is
        watching.  Counting the newlines in the published object answers the
        same question while the run is in flight: the store appends a whole
        line per event, so a terminated line is an event that is durable, and a
        partially written last line is correctly not counted yet.

        Fails open to ``0``.  This feeds a progress counter, and a progress
        counter that raises would take down the reconciler that also settles
        launches.
        """
        if self.watermark is not None:
            return self.watermark
        try:
            return self.read_bytes().count(b"\n")
        except Exception:
            log.warning("could not measure durable progress for %s", self.uri, exc_info=True)
            return 0

    def __str__(self) -> str:
        # ``project_events_to_trace`` records ``str(source)`` as the origin, so
        # a recovered trace names the artifact it came from rather than a path
        # on a machine the reader cannot see.
        return self.uri


def iter_event_sinks(
    store: ArtifactStore, launch_id: str, *, episode_id: str | None = None
) -> Iterator[EventSinkArtifact]:
    """Every durable event stream this launch published, newest last.

    Generic over the store: it needs only prefix enumeration and a read, which
    is the intersection of a directory tree and a bucket.
    """
    prefix = episode_prefix(launch_id, episode_id)
    for key in store.iter_keys(prefix):
        if not key.endswith(f"/{EVENTS_OBJECT}"):
            continue
        parts = key.split("/")
        if len(parts) < 3:  # pragma: no cover - defensive
            continue
        found_uid = unquote(parts[-2])
        found_episode_id = unquote(parts[-3])
        watermark, closed = _read_watermark(store, key)
        yield EventSinkArtifact(
            launch_id=launch_id,
            episode_id=found_episode_id,
            episode_uid=found_uid,
            uri=store.uri(key),
            store=store,
            watermark=watermark,
            closed=closed,
        )


def _read_watermark(store: ArtifactStore, events_key: str) -> tuple[int | None, bool]:
    """The sink's own account of how far it got, when it survived to write one.

    Fails open: an unreadable or absent watermark degrades to "unknown", which
    the caller replaces with the entry count.  Refusing here would discard a
    recoverable transcript over a missing annotation.
    """
    key = events_key.removesuffix(EVENTS_OBJECT) + WATERMARK_OBJECT
    try:
        payload = json.loads(store.get(store.uri(key)).decode("utf-8"))
    except Exception:
        return None, False
    if not isinstance(payload, dict):
        return None, False
    raw = payload.get("durable_through")
    return (int(raw) if isinstance(raw, int) else None), bool(payload.get("closed"))


def read_file_uri(uri: str) -> bytes:
    return local_path_for(uri).read_bytes()


def local_path_for(uri: str) -> Path:
    """The filesystem path a ``file://`` URI names.

    A bare path is accepted as well, because the local launcher and the local
    worker are the same machine during the transition and requiring a scheme
    there would be ceremony rather than a boundary.
    """
    parsed = urlparse(uri)
    if parsed.scheme in {"", "file"}:
        return Path(unquote(parsed.path) if parsed.scheme == "file" else uri)
    raise ValueError(f"{uri!r} is not a local artifact URI")


# --- backend registry -------------------------------------------------------

_BACKENDS: dict[str, Callable[..., ArtifactStore]] = {}
_READERS: dict[str, Callable[[str], bytes]] = {}


def register_artifact_store(
    name: str,
    factory: Callable[..., ArtifactStore],
    *,
    schemes: tuple[str, ...] = (),
    reader: Callable[[str], bytes] | None = None,
) -> None:
    """Register an artifact backend and, optionally, how to read its URIs.

    The reader is registered per scheme rather than per backend because the
    worker resolving a launch input knows only the URI: it was never told
    which store published it, and making it guess would reintroduce the
    coupling the reference exists to remove.
    """
    _BACKENDS[name] = factory
    if reader is not None:
        for scheme in schemes:
            _READERS[scheme] = reader


def list_artifact_stores() -> list[str]:
    return sorted(_BACKENDS)


def make_artifact_store(spec: dict[str, Any] | None, *, root: str | Path) -> ArtifactStore:
    """Build an artifact store from a resolved spec, mirroring ``make_store``."""
    spec = dict(spec or {})
    backend = spec.pop("backend", "local")
    if backend not in _BACKENDS:
        raise KeyError(
            f"Unknown artifact store backend {backend!r}. Known: {list_artifact_stores()}. "
            "Register one with a2a_engine.artifacts.register_artifact_store()."
        )
    spec.setdefault("root", root)
    return _BACKENDS[backend](**spec)


def fetch(uri: str) -> bytes:
    """Read an artifact by URI, without knowing which store published it."""
    scheme = urlparse(uri).scheme or "file"
    reader = _READERS.get(scheme)
    if reader is None:
        raise KeyError(
            f"No artifact reader registered for scheme {scheme!r} in {uri!r}. "
            f"Known schemes: {sorted(_READERS)}."
        )
    return reader(uri)


def fetch_verified(ref: ArtifactRef) -> bytes:
    """Read an artifact and refuse it unless the bytes match the digest."""
    data = fetch(ref.uri)
    actual = sha256_bytes(data)
    if actual != ref.sha256:
        raise ArtifactDigestMismatch(
            f"{ref.uri} hashes to {actual}, not the published {ref.sha256}"
        )
    return data


def materialize(ref: ArtifactRef, *, into: str | Path | None = None) -> Path:
    """Verify an artifact and return a local path a loader can open.

    A ``file://`` artifact is returned in place once verified.  That is not a
    shortcut around the boundary -- the digest was still checked -- and it is
    what keeps sidecar resolution (``presets.yaml``, ``storage.yaml``) and the
    agent-pool walk-up identical to a directly invoked run.
    """
    data = fetch_verified(ref)
    try:
        path = local_path_for(ref.uri)
    except ValueError:
        path = None
    if path is not None and path.is_file() and into is None:
        return path
    target = Path(into) if into is not None else Path(
        tempfile.mkdtemp(prefix="a2a-launch-input-")
    ) / Path(urlparse(ref.uri).path or "launch-input").name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


def copy_into(ref: ArtifactRef, destination: str | Path) -> Path:
    """Verify an artifact and write it to an explicit destination."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(fetch_verified(ref))
    return destination


register_artifact_store(
    "local", LocalArtifactStore, schemes=("file", ""), reader=read_file_uri
)

__all__ = [
    "ArtifactDigestMismatch",
    "ArtifactRef",
    "ArtifactStore",
    "EVENTS_OBJECT",
    "EventSinkArtifact",
    "LocalArtifactStore",
    "WATERMARK_OBJECT",
    "copy_into",
    "episode_prefix",
    "event_sink_key",
    "fetch",
    "fetch_verified",
    "iter_event_sinks",
    "list_artifact_stores",
    "local_path_for",
    "make_artifact_store",
    "materialize",
    "plan_key",
    "register_artifact_store",
    "sha256_bytes",
    "watermark_key",
]
