"""Required real-container gate; no Docker availability skip."""

import json
import subprocess

from .compose_support import run_compose
import time
import uuid

from .test_container_turns import ROOT, clean_environment


def test_word_guess_container_sessions_privacy_and_runner_viewer_readback():
    env = clean_environment()
    check = subprocess.run(["docker", "info"], env=env, capture_output=True, timeout=30)
    assert check.returncode == 0, "Phase 2 requires an available Docker daemon"
    project = "a2a-word-test-" + uuid.uuid4().hex[:12]
    command = ["docker", "compose", "--env-file", "/dev/null", "--profile", "agent-runtime", "-p", project,
               "-f", "docker-compose.yml", "-f", "docker-compose.agent-runtime.yml", "-f", "tests/runtime/word-guess.compose.yml"]

    def compose(*args, timeout=120):
        result = run_compose(command, args, cwd=ROOT, env=env, timeout=timeout)
        assert result.returncode == 0, f"Disposable Word Guess Compose command failed: {args[0]}\n{result.stderr}"
        return result.stdout

    services = ["runtime-environment", "runtime-agent", "runtime-host"]
    try:
        config = json.loads(compose("config", "--format", "json"))
        for name in services:
            assert not config["services"][name].get("ports") and not config["services"][name].get("env_file")
        for name in services[1:]:
            mounts = config["services"][name]["volumes"]
            assert len(mounts) == 1 and mounts[0]["read_only"]
        assert config["services"]["runtime-agent"]["volumes"][0]["source"] != config["services"]["runtime-host"]["volumes"][0]["source"]
        compose("build", "runtime-environment", "runtime-agent", timeout=1200)
        compose("up", "-d", "--wait", "--wait-timeout", "90", *services)
        deadline, status = time.monotonic() + 100, {}
        while time.monotonic() < deadline:
            status = json.loads(compose("exec", "-T", "runtime-environment", "python", "-c",
                "from urllib.request import urlopen; print(urlopen('http://runtime-environment:8000/status').read().decode())"))
            if status["done"]:
                break
            time.sleep(0.2)
        assert status == {"healthy": True, "done": True, "error": None, "episodes": 3, "actions": 24,
                          "private_registration": True, "persisted_readback": True, "workflow": "available"}
        for name in services[1:]:
            compose("exec", "-T", name, "python", "-c",
                "from pathlib import Path; import os; assert not Path('/data').exists(); "
                "assert not list(Path('/provision').glob('*.json')); "
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
