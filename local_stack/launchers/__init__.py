"""The execution boundary: submit an immutable launch input, then ask about it.

The launcher used to be a thing that *reported*.  It called back into two
private ``ControlPlane`` methods and fed a callback with text matching four
regexes tuned to ``expt_runner``'s ``logging.Formatter`` output.  None of that
survives a network boundary, so the contract inverts: a launcher is now a thing
that is *asked about*.

``submit`` takes the launch and a digest-bound reference to its input and
returns an opaque handle.  ``describe`` answers whether that handle still looks
alive -- a hint, never a verdict.  The verdict stays where it already was: the
control plane joining ``attempts`` against the ``episodes`` fact table.  A
worker that vanished without reporting therefore settles identically to one
that reported a success it could not persist.

Backends register by name exactly as ``a2a_engine.storage.make_store`` does, so
a container launcher or a Cloud Run Job launcher later is an additive import
rather than an ``if`` branch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, NamedTuple, Protocol

if TYPE_CHECKING:  # pragma: no cover - typing only, and the import is circular
    from local_stack.control_plane import Launch


#: ``--smoke-test`` exercises the sink and the environment wiring, so a worker
#: executes this many runs per cell rather than the cell's declared count.
#: It lives on the dispatch contract because the control plane must plan
#: exactly what the worker will execute; otherwise it would record episode
#: attempts that never ran.
SMOKE_EPISODES_PER_CELL = 1


class ExecutionStatus(str, Enum):
    """What the execution platform believes about one submitted handle.

    ``UNKNOWN`` is not an error state.  It is what a launcher says about a
    handle it cannot account for -- a process this instance did not start, a
    job the platform has forgotten -- and it is treated exactly like a terminal
    status, because in both cases the only remaining source of truth is the
    evidence the worker persisted.
    """

    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


#: Statuses after which nothing more will be produced by the execution.
TERMINAL_EXECUTION_STATUSES = frozenset({
    ExecutionStatus.SUCCEEDED,
    ExecutionStatus.FAILED,
    ExecutionStatus.CANCELLED,
})

#: Statuses after which the control plane must settle the launch from evidence.
SETTLEABLE_EXECUTION_STATUSES = TERMINAL_EXECUTION_STATUSES | {ExecutionStatus.UNKNOWN}


class LaunchInputRef(NamedTuple):
    """The one thing that crosses the boundary: where the plan is, and its digest.

    No workspace path, no experiment id, no control-plane URL.  A worker that
    can read this reference and verify it has everything it needs, and a worker
    that cannot verify it must refuse rather than run something else.
    """

    uri: str
    sha256: str


@dataclass(frozen=True)
class ExecutionHandle:
    """An opaque, persistable reference to one submitted execution.

    Persisted as JSON on the ``launches`` row, which is what replaces the
    in-memory ``_processes`` dict a restart used to lose and ``reconcile()``
    used to reach through the abstraction to read.
    """

    backend: str
    id: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {"backend": self.backend, "id": self.id, "detail": self.detail},
            separators=(",", ":"), sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw: str | None) -> "ExecutionHandle | None":
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict) or "backend" not in payload:
            return None
        return cls(
            backend=str(payload.get("backend") or ""),
            id=str(payload.get("id") or ""),
            detail=dict(payload.get("detail") or {}),
        )


class Launcher(Protocol):
    """Submit an execution, ask about it, ask it to stop."""

    name: str

    def submit(self, launch: "Launch", input_ref: LaunchInputRef) -> ExecutionHandle:
        """Start executing ``launch`` from ``input_ref`` and return a handle."""
        ...

    def describe(self, handle: ExecutionHandle) -> ExecutionStatus:
        """Report liveness for ``handle``. Never a verdict about results."""
        ...

    def cancel(self, handle: ExecutionHandle) -> bool:
        """Request termination. ``True`` means a stop was actually requested."""
        ...


# --- backend registry -------------------------------------------------------

_LAUNCHERS: dict[str, Callable[..., Launcher]] = {}


def register_launcher(name: str, factory: Callable[..., Launcher]) -> None:
    _LAUNCHERS[name] = factory


def list_launchers() -> list[str]:
    return sorted(_LAUNCHERS)


def make_launcher(spec: dict[str, Any] | None) -> Launcher:
    """Build a launcher from a resolved spec, mirroring ``make_store``.

        make_launcher({"backend": "local_process", "workspace": ...})

    Every remaining key becomes a constructor keyword, so a backend declares
    its own configuration instead of the control plane knowing about it.
    """
    spec = dict(spec or {})
    backend = spec.pop("backend", "local_process")
    if backend not in _LAUNCHERS:
        raise KeyError(
            f"Unknown launcher backend {backend!r}. Known: {list_launchers()}. "
            "Register one with local_stack.launchers.register_launcher()."
        )
    return _LAUNCHERS[backend](**spec)


def _register_builtin_launchers() -> None:
    """Import built-in launchers for their registration side effects."""
    from local_stack.launchers import local_process  # noqa: F401
    from local_stack.launchers import local_container  # noqa: F401


_register_builtin_launchers()

__all__ = [
    "ExecutionHandle",
    "ExecutionStatus",
    "LaunchInputRef",
    "Launcher",
    "SMOKE_EPISODES_PER_CELL",
    "SETTLEABLE_EXECUTION_STATUSES",
    "TERMINAL_EXECUTION_STATUSES",
    "list_launchers",
    "make_launcher",
    "register_launcher",
]
