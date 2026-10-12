"""Stdlib-only supervisor injected into independently pinned runtime images.

This file must remain executable without importing the infrastructure package.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        temporary = Path(out.name)
        out.write(json.dumps(value, sort_keys=True).encode())
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def confined(root, path):
    path = Path(path).resolve()
    if not path.is_relative_to(root):
        raise ValueError("execution path leaves shared volume")
    return path


def execute(root, pointer, stopped):
    run = confined(root, pointer["run_dir"])
    request = json.loads((run / "request.json").read_text())
    if request["id"] != pointer["id"]:
        raise ValueError("request identity mismatch")
    result = run / "result.json"
    if result.exists():
        return
    log_path = confined(run, request["log_path"])
    process = None
    code, error = 125, None
    try:
        for key, variable in (("source_sha", "PLATFORM_RUNTIME_SOURCE_SHA"), ("image_digest", "PLATFORM_RUNTIME_DIGEST")):
            expected = request.get("runtime_identity", {}).get(key)
            if expected and expected not in {"local", "test-source"} and os.getenv(variable) != expected:
                raise ValueError("pinned runtime identity mismatch for " + key)
        if (run / "cancel.json").exists() or stopped[0]:
            code = 130
            return
        # Secrets and runtime package paths belong to this pinned container.
        # The worker request contains only explicitly selected runner settings.
        env = dict(os.environ)
        env.update(request["env"])
        env["PYTHONPATH"] = str(run) + os.pathsep + os.environ.get("PYTHONPATH", "")
        env["PYTHONUNBUFFERED"] = "1"
        with log_path.open("ab", buffering=0) as log:
            process = subprocess.Popen([sys.executable, *request["argv"]], cwd=request["cwd"],
                env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True)
            next_heartbeat = 0
            while process.poll() is None:
                if time.monotonic() >= next_heartbeat:
                    atomic(run / "started.json", {"id": request["id"], "heartbeat": time.time(), "pid": process.pid})
                    next_heartbeat = time.monotonic() + 1
                worker_heartbeat = json.loads((run / "worker-heartbeat.json").read_text())
                infrastructure_stale = worker_heartbeat["id"] != request["id"] or time.time() - worker_heartbeat["heartbeat"] > request.get("worker_heartbeat_timeout", 30)
                if infrastructure_stale:
                    error = "infrastructure worker heartbeat expired; runtime cancelled"
                if stopped[0] or infrastructure_stale or (run / "cancel.json").exists():
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=float(request.get("cancel_grace", 10)))
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                    break
                time.sleep(.05)
            code = process.wait()
    except Exception as exc:
        error = f"runtime supervisor failed: {type(exc).__name__}: {exc}"
    finally:
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        atomic(result, {"id": request["id"], "exit_code": code, "error": error})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execution-dir", default="/execution")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--startup-timeout", type=float, default=120)
    args = parser.parse_args()
    root = Path(args.execution_dir).resolve()
    stopped = [False]
    def stop(*_):
        stopped[0] = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    last_id = None
    deadline = time.monotonic() + args.startup_timeout
    while not stopped[0]:
        if not args.loop and (root / "shutdown.json").exists():
            return 0
        try:
            pointer = json.loads((root / "request.json").read_text())
        except (FileNotFoundError, ValueError):
            pointer = None
        if pointer and pointer.get("id") != last_id:
            try:
                execute(root, pointer, stopped)
            except FileNotFoundError:
                # A completed worker cleaned an old pointer before rearming.
                pass
            last_id = pointer["id"]
            if not args.loop:
                return 0
        if not args.loop and time.monotonic() >= deadline:
            return 124
        time.sleep(.05)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
