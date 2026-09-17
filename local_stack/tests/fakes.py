"""One shared launcher double, with a real ``describe()`` state machine.

Every test file used to define its own stub -- ``SilentLauncher``,
``CompletingLauncher``, ``PartialLauncher``, ``FailingLauncher``,
``NoisyLauncher``, ``NullLauncher``, ``CapturingLauncher`` -- because a stub
only had to call two private control-plane methods and push strings at a
callback.  Now that ``describe()`` returns a meaningful state, every stub would
reimplement the same state machine, and reimplemented state machines drift.

The fake deliberately never reports a result.  It answers about liveness and
nothing else, which is exactly the contract: whether an episode exists is
settled by the control plane joining ``attempts`` against ``episodes``.
"""

from __future__ import annotations

from typing import Any, Callable

from local_stack.launchers import ExecutionHandle, ExecutionStatus, LaunchInputRef


class FakeLauncher:
    """A launcher that submits, remembers, and answers about liveness.

    ``on_submit`` runs at dispatch time with the launch and its input
    reference, which is where a test that wants to simulate a worker writes its
    episodes.  Returning an :class:`ExecutionStatus` from it makes the
    execution terminal immediately; returning ``None`` leaves it running until
    :meth:`finish` says otherwise.
    """

    name = "fake"

    def __init__(
        self,
        *,
        on_submit: Callable[[Any, LaunchInputRef], ExecutionStatus | None] | None = None,
        status: ExecutionStatus = ExecutionStatus.RUNNING,
    ) -> None:
        self.on_submit = on_submit
        self.initial_status = status
        self.submitted: list[tuple[Any, LaunchInputRef]] = []
        self.cancelled: list[str] = []
        self.statuses: dict[str, ExecutionStatus] = {}

    # -- contract ----------------------------------------------------------

    def submit(self, launch, input_ref: LaunchInputRef) -> ExecutionHandle:
        self.submitted.append((launch, input_ref))
        self.statuses[launch.id] = self.initial_status
        if self.on_submit is not None:
            outcome = self.on_submit(launch, input_ref)
            if outcome is not None:
                self.statuses[launch.id] = outcome
        return ExecutionHandle(
            backend=self.name, id=launch.id,
            detail={"launch_input_uri": input_ref.uri},
        )

    def describe(self, handle: ExecutionHandle) -> ExecutionStatus:
        # A handle this instance never issued is UNKNOWN, exactly as a restarted
        # process finds a subprocess it did not start. That is what makes the
        # restart-reconciliation tests exercise the real path.
        return self.statuses.get(handle.id, ExecutionStatus.UNKNOWN)

    def cancel(self, handle: ExecutionHandle) -> bool:
        if self.statuses.get(handle.id) not in {
            ExecutionStatus.SUBMITTED, ExecutionStatus.RUNNING,
        }:
            return False
        self.statuses[handle.id] = ExecutionStatus.CANCELLED
        self.cancelled.append(handle.id)
        return True

    # -- test affordances --------------------------------------------------

    def finish(self, launch_id: str, status: ExecutionStatus = ExecutionStatus.SUCCEEDED) -> None:
        """Make an execution terminal, as a worker exiting would."""
        self.statuses[launch_id] = status

    def last_input(self) -> LaunchInputRef:
        return self.submitted[-1][1]
