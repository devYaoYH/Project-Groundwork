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
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple, Protocol, runtime_checkable
from urllib.parse import unquote, urlparse


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

    def get(self, uri: str) -> bytes:
        """Read back bytes previously published by this store."""
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

    def get(self, uri: str) -> bytes:
        return read_file_uri(uri)

    def iter_keys(self, prefix: str = "") -> Iterator[str]:
        base = self._path(prefix) if prefix else self.root
        if not base.is_dir():
            return
        for path in sorted(base.rglob("*")):
            if path.is_file():
                yield path.relative_to(self.root).as_posix()


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
    "LocalArtifactStore",
    "copy_into",
    "fetch",
    "fetch_verified",
    "list_artifact_stores",
    "local_path_for",
    "make_artifact_store",
    "materialize",
    "register_artifact_store",
    "sha256_bytes",
]
