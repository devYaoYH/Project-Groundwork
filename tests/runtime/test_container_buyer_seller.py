"""Phase 4's required scripted and mock-provider real Docker gate; never skip."""

import json
import subprocess

from .compose_support import run_compose
import time
import uuid

import pytest

from .test_container_turns import ROOT, clean_environment


@pytest.mark.parametrize("mode,actions", [("scripted", 18), ("mock", 24)])
def test_buyer_seller_real_containers_privacy_parity_and_pending_round(mode, actions):
    env = {**clean_environment(), "BARGAIN_MODE": mode}
    try:
        check = subprocess.run(["docker", "info"], env=env, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pytest.fail("Phase 4 requires an available Docker daemon")
    assert check.returncode == 0, "Phase 4 requires an available Docker daemon"
    project = "a2a-bargain-test-" + uuid.uuid4().hex[:12]
    base = ["docker", "compose", "--env-file", "/dev/null", "--profile", "agent-runtime", "-p", project,
            "-f", "docker-compose.yml", "-f", "docker-compose.agent-runtime.yml"]
    command = [*base, "-f", "tests/runtime/buyer-seller.compose.yml"]

    def compose(*args, timeout=120, base_image=False):
        result = run_compose(base if base_image else command, args, cwd=ROOT, env=env,
                                timeout=timeout, fixture_agent=not base_image)
        assert result.returncode == 0, f"Disposable bargaining Compose command failed: {args[0]}\n{result.stderr}"
        return result.stdout

    def request(path, method="GET"):
        code = ("from urllib.request import Request,urlopen; "
                f"print(urlopen(Request('http://runtime-environment:8000/{path}', method='{method}'), timeout=3).read().decode())")
        return json.loads(compose("exec", "-T", "runtime-environment", "python", "-c", code))

    services = ["runtime-environment", "runtime-agent", "runtime-buyer"]
    try:
        config = json.loads(compose("config", "--format", "json"))
        assert config["networks"]["agent-runtime"]["internal"]
        for name in services:
            service = config["services"][name]
            assert not service.get("ports") and not service.get("env_file")
            assert set(service["environment"]) == {"OTEL_SDK_DISABLED", "LANGCHAIN_TRACING_V2", "BARGAIN_MODE"}
        for name in services[1:]:
            mounts = config["services"][name]["volumes"]
            assert len(mounts) == 1 and mounts[0]["read_only"]
        assert config["services"]["runtime-agent"]["volumes"][0]["source"] != config["services"]["runtime-buyer"]["volumes"][0]["source"]
        compose("build", "runtime-agent", base_image=True, timeout=1200)
        compose("build", "runtime-environment", "runtime-agent", timeout=1200)
        compose("up", "-d", "--wait", "--wait-timeout", "90", *services)
        until = time.monotonic() + 45
        while time.monotonic() < until:
            status = request("status")
            if status["held"] or status["done"]:
                break
            time.sleep(.1)
        assert status["held"] and not status["done"] and status["error"] is None
        assert request("release", "POST") == {"released": True}
        until = time.monotonic() + 90
        while time.monotonic() < until:
            status = request("status")
            if status["done"]:
                break
            time.sleep(.1)
        assert status == {"healthy": True, "done": True, "error": None, "held": True,
                          "episodes": 3, "actions": actions, "private": True, "persisted": True, "workflow": "available"}
        for name in services[1:]:
            compose("exec", "-T", name, "python", "-c",
                "from pathlib import Path; import os; assert not Path('/data').exists(); "
                "assert not Path('/workspace').exists(); assert not list(Path('/provision').glob('*.json')); "
                "assert not any(k.endswith('API_KEY') or 'TICKET' in k for k in os.environ)")
        logs = compose("logs", "--no-color", *services)
        assert not any(secret in logs for secret in ("join_ticket", "seat_secret", "lifecycle_credential", "Bearer "))
        compose("stop", *services)
        output = compose("ps", "--all", "--format", "json")
        records = json.loads(output) if output.lstrip().startswith("[") else [json.loads(line) for line in output.splitlines()]
        assert all(record["State"] == "exited" for record in records)
    finally:
        compose("down", "--volumes", "--remove-orphans")
        assert not compose("ps", "--all", "--quiet").strip()
