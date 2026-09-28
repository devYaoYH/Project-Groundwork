"""File-backed local dispatch to a separate runner container.

Only plan references, credential names, and operational paths cross this queue.
The control plane cannot read the worker's environment or run Docker.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from local_stack.launchers import ExecutionHandle, ExecutionStatus, LaunchInputRef, register_launcher

if TYPE_CHECKING:
    from local_stack.control_plane import Launch


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


class WorkerServiceLauncher:
    name = "worker_service"

    def __init__(self, workspace: str | Path, *, artifact_root: str | Path,
                 credentials: list[str] | None = None) -> None:
        self.workspace = Path(workspace)
        self.root = Path(artifact_root).resolve() / "dispatch"
        self.credentials = tuple(credentials or ())

    def credential_presence(self, names: list[str]) -> dict[str, bool]:
        try:
            report = json.loads((self.root / "presence.json").read_text())
            if time.time() - report["updated_at"] > 15:
                return {name: False for name in names}
            return {name: report["credentials"].get(name, False) is True for name in names}
        except (OSError, ValueError, KeyError, TypeError):
            return {name: False for name in names}

    def submit(self, launch: "Launch", input_ref: LaunchInputRef) -> ExecutionHandle:
        if launch.provenance_grade != "unverified" or launch.image_digest:
            raise ValueError("the shared worker service cannot claim a pinned image digest")
        if not (self.root / "presence.json").is_file():
            raise RuntimeError("worker service is not ready")
        atomic_json(self.root / f"{launch.id}.request.json", {
            "launch_id": launch.id, "input_uri": input_ref.uri, "input_sha256": input_ref.sha256,
            "mode": launch.mode, "shard_count": launch.shard_count or 1,
            "max_parallelism": launch.max_parallelism,
        })
        return ExecutionHandle(backend=self.name, id=launch.id)

    def describe(self, handle: ExecutionHandle) -> ExecutionStatus:
        try:
            state = json.loads((self.root / f"{handle.id}.status.json").read_text())
        except (OSError, ValueError):
            queued = any((self.root / f"{handle.id}.{suffix}.json").exists()
                         for suffix in ("request", "claimed"))
            return ExecutionStatus.SUBMITTED if queued else ExecutionStatus.UNKNOWN
        status = state.get("status")
        if status == "RUNNING" and time.time() - state.get("updated_at", 0) > 15:
            return ExecutionStatus.UNKNOWN
        try:
            return ExecutionStatus(status)
        except ValueError:
            return ExecutionStatus.UNKNOWN

    def cancel(self, handle: ExecutionHandle) -> bool:
        if self.describe(handle) not in {ExecutionStatus.SUBMITTED, ExecutionStatus.RUNNING}:
            return False
        atomic_json(self.root / f"{handle.id}.cancel.json", {"requested_at": time.time()})
        return True


register_launcher("worker_service", WorkerServiceLauncher)
