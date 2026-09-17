"""A local subprocess worker that obeys the remote contract.

The problem with the old ``LocalLauncher`` was never that it used a
subprocess.  It was that the subprocess inherited ``dict(os.environ)``
wholesale -- every provider key the always-on service happened to hold -- and
reached back into two private ``ControlPlane`` methods.  Both are local-only
privileges a Cloud Run task could not have, so the boundary they hid stayed
untested.

This launcher keeps the subprocess and drops the privileges:

* the child's environment is an explicit allowlist of operational variables,
  never the parent's environment, so a provider credential can only reach a
  worker by being named;
* the launch input crosses as a URI plus a digest, and the worker verifies it
  before parsing;
* nothing is reported back.  The control plane reconciles.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from local_stack.launchers import (
    SMOKE_EPISODES_PER_CELL,
    ExecutionHandle,
    ExecutionStatus,
    LaunchInputRef,
    register_launcher,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from local_stack.control_plane import Launch


#: Operational variables a worker needs to start at all: how to find an
#: interpreter, where to put temporary files, and which locale to decode in.
#: Deliberately short, and deliberately free of anything that authenticates to
#: anything. A provider credential reaches a worker only by being passed
#: explicitly as ``credentials``, which is the seam a symbolic credential
#: reference replaces later.
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "USERPROFILE",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONUNBUFFERED",
    "VIRTUAL_ENV",
    # Where the agent pool is read from; a path, never a secret.
    "A2A_AGENT_POOL",
    # Live progress is best-effort and TTL-bound; the URL never enters any
    # persisted experiment or trace metadata.
    "A2A_REDIS_URL",
)

#: Names that must never be forwarded implicitly, even if an allowlist grows
#: carelessly. Belt and braces: the allowlist above already excludes them, and
#: this is what a test can point at.
PROVIDER_CREDENTIAL_NAMES: frozenset[str] = frozenset({
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "OLLAMA_API_KEY",
})


class LocalProcessLauncher:
    """Run one launch as a bounded local child process.

    ``workspace`` is the launcher's own configuration -- the worker's working
    directory -- not something the control plane hands it per launch. The
    launch input is the plan reference and nothing else.
    """

    name = "local_process"

    def __init__(
        self,
        workspace: str | Path,
        *,
        credentials: Mapping[str, str] | None = None,
        env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST,
        python: str | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        # The worker's credential environment, supplied explicitly. Empty by
        # default: an always-on control plane that never read a provider key
        # cannot leak one, and the local inner loop gets its keys back through
        # the same named-credential path a hosted worker will use.
        self.credentials = dict(credentials or {})
        self.env_allowlist = tuple(env_allowlist)
        self.python = python or sys.executable
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    # -- contract ----------------------------------------------------------

    def submit(self, launch: "Launch", input_ref: LaunchInputRef) -> ExecutionHandle:
        command = self._command(launch, input_ref)
        # Worker output is inherited rather than piped. There is no watcher
        # thread to read it any more, and a remote worker's logs belong to its
        # platform: the control plane learns what happened from the evidence
        # the worker persisted, not from its stdout.
        process = subprocess.Popen(
            command,
            cwd=self.workspace,
            env=self._environment(launch),
        )
        with self._lock:
            self._processes[launch.id] = process
        return ExecutionHandle(
            backend=self.name,
            id=launch.id,
            detail={"pid": process.pid, "launch_input_uri": input_ref.uri},
        )

    def describe(self, handle: ExecutionHandle) -> ExecutionStatus:
        with self._lock:
            process = self._processes.get(handle.id)
            cancelled = handle.id in self._cancelled
        if process is None:
            # A handle this instance never started, or one a restart lost.
            # Either way the process tree cannot answer, and the episodes fact
            # table can.
            return ExecutionStatus.UNKNOWN
        code = process.poll()
        if code is None:
            return ExecutionStatus.RUNNING
        with self._lock:
            self._processes.pop(handle.id, None)
            self._cancelled.discard(handle.id)
        if cancelled:
            return ExecutionStatus.CANCELLED
        return ExecutionStatus.SUCCEEDED if code == 0 else ExecutionStatus.FAILED

    def cancel(self, handle: ExecutionHandle) -> bool:
        with self._lock:
            process = self._processes.get(handle.id)
            if process is None or process.poll() is not None:
                return False
            self._cancelled.add(handle.id)
        process.terminate()
        return True

    # -- construction ------------------------------------------------------

    def _command(self, launch: "Launch", input_ref: LaunchInputRef) -> list[str]:
        command = [
            self.python,
            "-m",
            "expt_runner.run_experiment",
            "--launch-input",
            input_ref.uri,
            "--launch-input-sha256",
            input_ref.sha256,
            "--storage-path",
            launch.trace_database,
            "--max-parallelism",
            str(launch.max_parallelism),
            # The workspace may be mounted read-only so host edits are live;
            # run artifacts belong beside the trace database regardless.
            "--results-dir",
            str(self._results_dir(launch)),
        ]
        if launch.mode == "smoke":
            command += [
                "--smoke-test",
                "--smoke-episodes-per-cell",
                str(SMOKE_EPISODES_PER_CELL),
            ]
        if launch.mode == "dry_run":
            command.append("--dry-run")
        return command

    @staticmethod
    def _results_dir(launch: "Launch") -> Path:
        return Path(launch.trace_database).parent / "results"

    def _environment(self, launch: "Launch") -> dict[str, str]:
        env = {
            name: os.environ[name]
            for name in self.env_allowlist
            if name in os.environ and name not in PROVIDER_CREDENTIAL_NAMES
        }
        env.setdefault("A2A_CAPTURE_CONTENT", "true")
        env.setdefault("OTEL_TRACES_EXPORTER", "file")
        env.setdefault(
            "A2A_OTEL_TRACES_FILE",
            str(Path(launch.trace_database).with_name("otel-spans.jsonl")),
        )
        # A launch uses one deterministic stream namespace; each runner context
        # appends its own episode suffix.
        if env.get("A2A_REDIS_URL"):
            env["A2A_LAUNCH_ID"] = launch.id
            env["A2A_REDIS_STREAM_PREFIX"] = f"a2a:launch:{launch.id}"
        env.update(self.credentials)
        return env


def _factory(**spec: Any) -> LocalProcessLauncher:
    workspace = spec.pop("workspace", None)
    if workspace is None:
        raise KeyError("the local_process launcher needs a workspace")
    return LocalProcessLauncher(workspace, **spec)


register_launcher("local_process", _factory)
