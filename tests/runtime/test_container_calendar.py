"""Required /2 Docker gate for Calendar decisions, callbacks and reflection scheduling."""

import json
import subprocess
import time
import uuid

from .test_container_turns import ROOT, clean_environment


def test_calendar_container_subgraphs_callbacks_and_frozen_driver_parity():
    env = clean_environment()
    check = subprocess.run(["docker", "info"], env=env, capture_output=True, timeout=30)
    assert check.returncode == 0, "Phase 3 requires an available Docker daemon"
    project = "a2a-calendar-test-" + uuid.uuid4().hex[:12]
    base = ["docker", "compose", "--env-file", "/dev/null", "--profile", "agent-runtime", "-p", project,
            "-f", "docker-compose.yml", "-f", "docker-compose.agent-runtime.yml"]
    command = [*base, "-f", "tests/runtime/calendar.compose.yml"]

    def compose(*args, timeout=120, base_image=False):
        output = subprocess.run([*(base if base_image else command), *args], cwd=ROOT, env=env,
                                capture_output=True, text=True, timeout=timeout)
        assert output.returncode == 0, f"Disposable Calendar Compose command failed: {args[0]}\n{output.stderr}"
        return output.stdout

    def request(path, method="GET"):
        code = ("from urllib.request import Request,urlopen; "
                f"print(urlopen(Request('http://runtime-environment:8000/{path}',method='{method}'),timeout=3).read().decode())")
        return json.loads(compose("exec", "-T", "runtime-environment", "python", "-c", code))

    services = ["runtime-environment", "runtime-agent", "runtime-host", "runtime-observer"]
    try:
        config = json.loads(compose("config", "--format", "json"))
        mounts = []
        for name in services:
            assert not config["services"][name].get("ports") and not config["services"][name].get("env_file")
            if name != "runtime-environment":
                volume = config["services"][name]["volumes"]
                assert len(volume) == 1 and volume[0]["read_only"]
                mounts.append(volume[0]["source"])
        assert len(set(mounts)) == 3 and config["networks"]["agent-runtime"]["internal"]
        compose("build", "runtime-agent", base_image=True, timeout=1200)
        compose("build", "runtime-environment", "runtime-agent", timeout=1200)
        compose("up", "-d", "--wait", "--wait-timeout", "90", *services)
        deadline = time.monotonic() + 30
        pending = {}
        while time.monotonic() < deadline:
            pending = request("status")
            if pending["accepted_pending"] and pending["staged"] == 1:
                break
            assert not pending["done"], pending
            time.sleep(0.1)
        assert pending["accepted_pending"] and pending["staged"] == 1 and not pending["closed"] and not pending["done"]
        time.sleep(2.5)
        waiting = request("status")
        assert waiting["accepted_pending"] and waiting["tool_calls"] == 4
        assert request("release", "POST")["released"]
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline:
            status = request("status")
            if status["done"]:
                break
            time.sleep(0.1)
        assert status["done"] and status["error"] is None, json.dumps(status)
        assert {key: status[key] for key in ("episodes", "reflections", "parity", "persisted_readback", "workflow")} == {
            "episodes": 2, "reflections": 12, "parity": True, "persisted_readback": True, "workflow": "partial"}
        for name in services[1:]:
            compose("exec", "-T", name, "python", "-c", "from pathlib import Path; import os; "
                "assert not Path('/data').exists(); assert not list(Path('/provision').glob('*.json')); "
                "assert not any(k.endswith('API_KEY') for k in os.environ)")
        logs = compose("logs", "--no-color", *services)
        assert not any(secret in logs for secret in ("join_ticket", "seat_secret", "lifecycle_credential", "Bearer "))
        compose("stop", *services)
        output = compose("ps", "--all", "--format", "json")
        records = json.loads(output) if output.lstrip().startswith("[") else [json.loads(line) for line in output.splitlines()]
        assert all(record["State"] == "exited" for record in records)
    finally:
        compose("down", "--volumes", "--remove-orphans")
        assert not compose("ps", "--all", "--quiet").strip()
