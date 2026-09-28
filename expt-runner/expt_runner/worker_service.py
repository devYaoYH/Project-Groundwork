"""Consume local dispatch requests in the runner's credentialed container."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import yaml

from a2a_engine.agent_pool import load_agent_pool
from a2a_engine.artifacts import ArtifactRef, fetch_verified
from local_stack.launchers.worker_service import atomic_json


def serve(*, workspace: Path, data: Path, artifact_root: Path, interval: float = 0.25) -> None:
    root = artifact_root / "dispatch"
    root.mkdir(parents=True, exist_ok=True)
    pool = load_agent_pool(workspace / "experiments")
    declared = pool.required_credentials(sorted(pool.agents))

    def presence() -> None:
        atomic_json(root / "presence.json", {
            "updated_at": time.time(),
            "credentials": {name: bool(os.environ.get(name)) for name in declared},
        })

    def status(launch_id: str, state: str) -> None:
        atomic_json(root / f"{launch_id}.status.json", {
            "status": state, "updated_at": time.time(),
        })

    while True:
        presence()
        for request in sorted(root.glob("*.request.json")):
            claimed = request.with_name(request.name.replace(".request.json", ".claimed.json"))
            try:
                request.rename(claimed)
            except OSError:
                continue
            launch_id = claimed.name.removesuffix(".claimed.json")
            children: list[subprocess.Popen] = []
            try:
                job = json.loads(claimed.read_text(encoding="utf-8"))
                if job["launch_id"] != launch_id or not re.fullmatch(r"[0-9a-f-]{36}", launch_id):
                    raise ValueError("invalid launch identity")
                ref = ArtifactRef(uri=job["input_uri"], sha256=job["input_sha256"])
                source = Path(ref.uri.removeprefix("file://")).resolve()
                if not ref.uri.startswith("file://") or not (
                    source.is_relative_to(artifact_root.resolve()) or source.is_relative_to(workspace.resolve())
                ):
                    raise ValueError("launch input must be within the artifact or workspace mount")
                plan = yaml.safe_load(fetch_verified(ref))
                names = plan.get("credentials", [])
                if (not isinstance(names, list) or any(not isinstance(name, str) for name in names)
                        or not set(names) <= set(declared)):
                    raise ValueError("launch input names an undeclared credential")
                missing = pool.missing_credentials(
                    [name for name in pool.agents if pool.agents[name].credential in names],
                    {name: bool(os.environ.get(name)) for name in names},
                )
                if job["mode"] == "live" and missing:
                    raise ValueError(f"missing worker credentials: {', '.join(missing)}")
                status(launch_id, "RUNNING")
                for index in range(job["shard_count"]):
                    env = {key: os.environ[key] for key in (
                        "PATH", "HOME", "PYTHONPATH", "A2A_REDIS_URL", "A2A_AGENT_POOL",
                        "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION",
                    ) if key in os.environ}
                    env.update({name: os.environ[name] for name in names if os.environ.get(name)})
                    env.update({
                        "CLOUD_RUN_TASK_INDEX": str(index),
                        "CLOUD_RUN_TASK_COUNT": str(job["shard_count"]),
                        "CLOUD_RUN_TASK_ATTEMPT": "0",
                        "A2A_LAUNCH_ID": launch_id,
                        "A2A_REDIS_STREAM_PREFIX": f"a2a:launch:{launch_id}",
                        "OTEL_TRACES_EXPORTER": "file",
                        "A2A_OTEL_TRACES_FILE": str(data / "otel-spans.jsonl"),
                        "A2A_CAPTURE_CONTENT": "true",
                    })
                    command = [sys.executable, "-m", "expt_runner.run_experiment",
                               "--launch-input", ref.uri, "--launch-input-sha256", ref.sha256,
                               "--storage-path", str(data / "a2a.db"),
                               "--artifact-root", str(artifact_root), "--launch-id", launch_id,
                               "--results-dir", str(data / "worker-results" / launch_id),
                               "--max-parallelism", str(job["max_parallelism"])]
                    if job["mode"] == "smoke":
                        command.extend(["--smoke-test", "--smoke-episodes-per-cell", "1"])
                    if job["mode"] == "dry_run":
                        command.append("--dry-run")
                    children.append(subprocess.Popen(command, cwd=workspace, env=env))
                cancelled = False
                while any(child.poll() is None for child in children):
                    presence()
                    status(launch_id, "RUNNING")
                    if (root / f"{launch_id}.cancel.json").exists():
                        cancelled = True
                        for child in children:
                            if child.poll() is None:
                                child.terminate()
                    time.sleep(interval)
                status(launch_id, "CANCELLED" if cancelled else
                       "SUCCEEDED" if all(child.wait() == 0 for child in children) else "FAILED")
            except Exception as exc:
                print(f"worker dispatch {launch_id} failed: {exc}", file=sys.stderr, flush=True)
                for child in children:
                    if child.poll() is None:
                        child.terminate()
                    child.wait()
                status(launch_id, "FAILED")
            finally:
                claimed.unlink(missing_ok=True)
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path("/workspace"))
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--artifact-root", type=Path, default=Path("/data/artifacts"))
    args = parser.parse_args()
    serve(workspace=args.workspace, data=args.data, artifact_root=args.artifact_root)


if __name__ == "__main__":
    main()
