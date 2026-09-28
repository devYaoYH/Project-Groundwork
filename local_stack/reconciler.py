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

It is also the only source of *progress*.  A pass publishes how far each
attempt's durable evidence has reached before it decides whether the launch is
over, which makes live progress the same derivation as launch truth on a
shorter timer -- and keeps the worker with nothing to push and no credential to
push it with.
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
        # The last mark published per ``(launch_id, episode_id)``, so a pass
        # that observes nothing new writes nothing. Deliberately in memory: it
        # is a de-duplication cache, not a record, and the record it would
        # duplicate is already in ``launch_events``. A restarted process
        # re-publishes each episode's current mark once, which is a frame
        # saying exactly what the durable log already said.
        self._marks: dict[tuple[str, str], tuple[str, int]] = {}

    def tick(self) -> list[str]:
        """Advance every open launch once. Returns the launches settled."""
        settled: list[str] = []
        for launch in self.control._open_launches():
            # Before settling, not after: a launch that finishes during this
            # pass still gets its last progress frame ahead of the terminal
            # one, so a subscriber never sees a launch complete from a counter
            # that stopped short.
            self._publish_progress(launch)
            if self._tick_launch(launch):
                settled.append(launch.id)
        return settled

    def _publish_progress(self, launch) -> None:
        """Turn advanced durable evidence into ``attempt.progress`` events.

        This is the whole of live progress. There is no worker push and no
        second transport: the control plane reads the artifacts it can already
        reach and writes what it found into the log the browser subscribes to,
        which is why watching a launch needs no credential in the worker and no
        inbound route to the control plane.
        """
        try:
            observed = self.control.durable_progress(launch)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("progress for %s failed: %s", launch.id, exc)
            return
        advanced = []
        for row in observed:
            key = (launch.id, str(row["episode_id"]))
            mark = (str(row["status"]), int(row["durable_through"]))
            if self._marks.get(key) == mark:
                continue
            self._marks[key] = mark
            advanced.append(row)
        self.control.record_attempt_progress(launch.id, advanced)

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
