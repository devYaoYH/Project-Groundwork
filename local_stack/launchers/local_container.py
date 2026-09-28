"""Digest-pinned Docker worker; never executes the viewer's installed game code."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from local_stack.launchers import ExecutionHandle, ExecutionStatus, LaunchInputRef, register_launcher
from local_stack.launchers.local_process import SMOKE_EPISODES_PER_CELL, WORKER_RESULTS_DIRNAME

if TYPE_CHECKING:
    from local_stack.control_plane import Launch


class LocalContainerLauncher:
    name = "local_container"

    def __init__(self, workspace: str | Path, *, artifact_root: str | Path,
                 credentials: list[str] | None = None) -> None:
        self.workspace = Path(workspace)
        self.artifact_root = Path(artifact_root).resolve()
        self.credentials = tuple(credentials or ())

    @staticmethod
    def _docker(*args: str) -> str:
        return subprocess.check_output(["docker", *args], text=True).strip()

    def submit(self, launch: "Launch", input_ref: LaunchInputRef) -> ExecutionHandle:
        digest = launch.image_digest
        if launch.provenance_grade != "verified" or not digest:
            raise ValueError("container execution requires a registered image digest")
        if self._docker("image", "inspect", digest, "--format", "{{.Id}}") != digest:
            raise ValueError("local image does not match the registered digest")
        data = Path(launch.trace_database).resolve().parent
        if not self.artifact_root.is_relative_to(data):
            raise ValueError("artifact root must be under the mounted data directory")
        if not input_ref.uri.startswith("file://") or not Path(input_ref.uri.removeprefix("file://")).is_relative_to(data):
            raise ValueError("verified container launch requires a published plan under the artifact root")
        ids: list[str] = []
        try:
            for index in range(launch.shard_count or 1):
                command = ["docker", "run", "-d", "--entrypoint", "python", "--mount",
                           f"type=bind,src={data},dst={data}", "--workdir", "/workspace",
                           "-e", f"A2A_EXECUTION_IMAGE_DIGEST={digest}",
                           "-e", f"CLOUD_RUN_TASK_INDEX={index}",
                           "-e", f"CLOUD_RUN_TASK_COUNT={launch.shard_count or 1}",
                           "-e", "CLOUD_RUN_TASK_ATTEMPT=0"]
                # Only declared credentials travel, and their values never occur in argv.
                for name in self.credentials:
                    command.extend(["-e", name])
                command.extend([digest, "-m", "expt_runner.run_experiment",
                                "--launch-input", input_ref.uri, "--launch-input-sha256", input_ref.sha256,
                                "--storage-path", str(Path(launch.trace_database).resolve()),
                                "--artifact-root", str(self.artifact_root), "--launch-id", launch.id,
                                "--max-parallelism", str(launch.max_parallelism),
                                "--results-dir", str(data / WORKER_RESULTS_DIRNAME / launch.id)])
                if launch.mode == "smoke":
                    command.extend(["--smoke-test", "--smoke-episodes-per-cell", str(SMOKE_EPISODES_PER_CELL)])
                if launch.mode == "dry_run":
                    command.append("--dry-run")
                ids.append(subprocess.check_output(command, text=True).strip())
        except Exception:
            for container_id in ids:
                self._docker("stop", container_id)
            raise
        return ExecutionHandle(backend=self.name, id=launch.id, detail={"containers": ids, "image_digest": digest})

    def describe(self, handle: ExecutionHandle) -> ExecutionStatus:
        ids = handle.detail.get("containers", [])
        if not ids:
            return ExecutionStatus.UNKNOWN
        states = []
        for container_id in ids:
            try:
                states.append(json.loads(self._docker("inspect", container_id, "--format", "{{json .State}}")))
            except subprocess.CalledProcessError:
                return ExecutionStatus.UNKNOWN
        if any(state.get("Running") for state in states):
            return ExecutionStatus.RUNNING
        if any(state.get("Status") not in {"exited", "dead"} for state in states):
            return ExecutionStatus.SUBMITTED
        return (ExecutionStatus.SUCCEEDED if all(state.get("ExitCode") == 0 for state in states)
                else ExecutionStatus.FAILED)

    def cancel(self, handle: ExecutionHandle) -> bool:
        if self.describe(handle) not in {ExecutionStatus.RUNNING, ExecutionStatus.SUBMITTED}:
            return False
        for container_id in handle.detail.get("containers", []):
            self._docker("stop", container_id)
        return True

    def credential_presence(self, names: list[str]) -> dict[str, bool]:
        return {name: False for name in names}


def _factory(**spec: Any) -> LocalContainerLauncher:
    return LocalContainerLauncher(**spec)


register_launcher("local_container", _factory)
