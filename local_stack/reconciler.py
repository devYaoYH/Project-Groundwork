"""The loop that owns every status transition.

Nothing else moves a launch out of ``QUEUED``/``RUNNING``/``CANCELLING``.  The
launcher is consulted only for liveness; whether an episode exists is settled
by joining ``attempts`` against the ``episodes`` fact table, which is the same
query the control plane already trusted and the only one that survives a worker
on another machine.

That inversion is what makes a crashed worker and a worker that reported a
success it could not persist settle identically: in both cases the process is
gone and the evidence is all there is.  It is also why this is a plain
``tick()`` rather than a thread -- reconciliation is idempotent and cheap, so
it runs from construction and from every read that asks about a launch, and a
background cadence can be layered on later without changing any outcome.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from local_stack.launchers import (
    SETTLEABLE_EXECUTION_STATUSES,
    ExecutionHandle,
    ExecutionStatus,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from local_stack.control_plane import ControlPlane

log = logging.getLogger("local_stack.reconciler")

#: Launch statuses the reconciler is responsible for moving.
OPEN_LAUNCH_STATUSES = ("QUEUED", "RUNNING", "CANCELLING")


class Reconciler:
    """Settle every open launch from the evidence its worker persisted."""

    def __init__(self, control: "ControlPlane") -> None:
        self.control = control

    def tick(self) -> list[str]:
        """Advance every open launch once. Returns the launches settled."""
        settled: list[str] = []
        for launch in self.control._open_launches():
            if self._tick_launch(launch):
                settled.append(launch.id)
        return settled

    def _tick_launch(self, launch) -> bool:
        handle = ExecutionHandle.from_json(launch.execution_handle)
        status = self._describe(handle)
        if status not in SETTLEABLE_EXECUTION_STATUSES:
            return False
        self.control._settle_from_evidence(launch, execution=status)
        return True

    def _describe(self, handle: ExecutionHandle | None) -> ExecutionStatus:
        if handle is None:
            # A launch with no handle was never dispatched -- the process died
            # between the insert and the submit, or an older row predates the
            # column. Either way nothing is running it.
            return ExecutionStatus.UNKNOWN
        try:
            return ExecutionStatus(self.control.launcher.describe(handle))
        except Exception as exc:  # pragma: no cover - defensive
            # Liveness is an observability read, so it degrades rather than
            # raising; UNKNOWN routes it straight to the evidence join, which
            # is the honest answer when the platform cannot be asked.
            log.warning("describe(%s) failed: %s", handle.id, exc)
            return ExecutionStatus.UNKNOWN
