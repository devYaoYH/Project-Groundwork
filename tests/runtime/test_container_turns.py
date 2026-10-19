"""Real Docker acceptance gate: unavailable Docker is an error, never a skip."""

import json
import os
import subprocess

from .compose_support import run_compose
import time
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SERVICES = ["runtime-environment", "runtime-agent"]


def clean_environment():
    return {key: os.environ[key] for key in ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "TMPDIR", "GROUNDWORK_CI_PREBUILT_IMAGES") if key in os.environ}


@pytest.mark.parametrize("runtime_loss", [False, True])
def test_real_container_acknowledgement_and_opposite_mcp_completion(runtime_loss):
    env = clean_environment()
    try:
        available = subprocess.run(["docker", "info"], env=env, capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pytest.fail("Phase 1 requires an available Docker daemon")
    assert available.returncode == 0, "Phase 1 requires an available Docker daemon"
    project = "a2a-turn-test-" + uuid.uuid4().hex[:12]
    command = ["docker", "compose", "--env-file", "/dev/null", "--profile", "agent-runtime", "-p", project,
               "-f", "docker-compose.yml", "-f", "docker-compose.agent-runtime.yml"]

    def compose(*arguments, timeout=120):
        result = run_compose(command, arguments, cwd=ROOT, env=env, timeout=timeout)
        assert result.returncode == 0, f"Disposable Compose command failed: {arguments[0]}\n{result.stderr}"
        return result.stdout

    def request(path, method="GET"):
        code = ("import json; from urllib.request import Request,urlopen; "
                f"print(urlopen(Request('http://runtime-environment:8000/{path}', method='{method}'), timeout=3).read().decode())")
        return json.loads(compose("exec", "-T", "runtime-environment", "python", "-c", code))

    def wait_for(predicate, *, timeout=20):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = request("status")
            if predicate(last):
                return last
            time.sleep(0.1)
        pytest.fail(f"Container lifecycle condition timed out: {last}")

    try:
        config = json.loads(compose("config", "--format", "json"))
        for name in SERVICES:
            service = config["services"][name]
            assert not service.get("ports") and not service.get("env_file")
            assert set(service["environment"]) == {"OTEL_SDK_DISABLED", "LANGCHAIN_TRACING_V2"}
        mount = config["services"]["runtime-agent"]["volumes"]
        assert len(mount) == 1 and mount[0]["read_only"]
        assert config["networks"]["agent-runtime"]["internal"]
        compose("build", *SERVICES, timeout=1200)
        compose("up", "-d", "--wait", "--wait-timeout", "90", *SERVICES, timeout=120)
        pending = wait_for(lambda s: s.get("accepted") and s.get("staged") == 1)
        assert not pending["done"] and not pending["closed"] and pending["worker_calls"] == 1
        # Hold the admitted long turn well beyond the 0.5s start request timeout.
        time.sleep(1.5)
        still_pending = request("status")
        assert still_pending["accepted"] and not still_pending["closed"] and not still_pending["done"]
        assert still_pending["snapshot"] is None
        finishing_started = time.monotonic()
        if runtime_loss:
            compose("kill", "runtime-agent")
        else:
            assert request("release", "POST")["released"]
        finished = wait_for(lambda s: s["done"], timeout=25)
        if runtime_loss:
            # A 15s turn deadline must not incur another 15s end-delivery wait.
            assert time.monotonic() - finishing_started < 20
        assert finished["error"] is None and finished["closed_by"] == ("deadline" if runtime_loss else "completed")
        assert finished["result"] == {"registered": True, "completed": not runtime_loss, "actions": [{"action": {"value": "staged"}}]}
        assert finished["snapshot"] == finished["result"]["actions"] and finished["worker_calls"] == 1
        if not runtime_loss:
            # episode_end ends only this session; the agent process remains healthy.
            healthy = compose("exec", "-T", "runtime-agent", "python", "-c",
                              "from urllib.request import urlopen; print(urlopen('http://runtime-agent:8001/health').read().decode())")
            assert json.loads(healthy)["healthy"]
            isolation = compose("exec", "-T", "runtime-agent", "python", "-c",
                            "from pathlib import Path; import os; "
                            "assert not Path('/data').exists(); assert not Path('/workspace').exists(); "
                            "assert not list(Path('/provision').glob('*.json')); "
                            "assert not any(k.endswith('API_KEY') or 'TICKET' in k for k in os.environ); "
                            "print('isolated')")
            assert isolation.strip() == "isolated"
            readonly = compose("exec", "-T", "runtime-agent", "python", "-c",
                           "import errno; from pathlib import Path; "
                           "exec(\"try:\\n Path('/provision/forbidden').touch()\\nexcept OSError as error:\\n assert error.errno == errno.EROFS\\nelse:\\n raise AssertionError('provisioning mount is writable')\")")
            assert not readonly.strip()
        serialized = json.dumps(finished) + compose("logs", "--no-color", *SERVICES)
        assert all(secret not in serialized for secret in ("seat_secret", "join_ticket", "capability", "lifecycle_credential"))
        compose("stop", *SERVICES)
        output = compose("ps", "--all", "--format", "json")
        processes = json.loads(output) if output.lstrip().startswith("[") else [json.loads(line) for line in output.splitlines()]
        if isinstance(processes, dict):
            processes = [processes]
        assert all(process["State"] == "exited" for process in processes)
    finally:
        compose("down", "--volumes", "--remove-orphans", timeout=120)
        assert not compose("ps", "--all", "--quiet").strip(), "Disposable containers survived teardown"
