import json
import subprocess

from .compose_support import run_compose
import uuid
from urllib.request import urlopen

from .test_container_turns import ROOT, clean_environment


def test_supported_cli_viewer_launches_and_agent_only_provider_configuration():
    env = clean_environment()
    assert subprocess.run(["docker", "info"], env=env, capture_output=True, timeout=30).returncode == 0
    project = "a2a-launch-test-" + uuid.uuid4().hex[:12]
    command = ["docker", "compose", "--env-file", "/dev/null", "-p", project,
               "-f", "docker-compose.runtime-launch.yml", "-f", "tests/runtime/launch.compose.yml"]

    def compose(*args, timeout=120):
        result = run_compose(command, args, cwd=ROOT, env=env, timeout=timeout)
        assert result.returncode == 0, f"Isolated launch gate failed: {args[0]}\n{result.stdout}\n{result.stderr}"
        return result.stdout

    try:
        config = json.loads(compose("config", "--format", "json"))
        assert config["networks"]["runtime"]["internal"]
        assert set(config["services"]["viewer"]["networks"]) == {"runtime", "viewer-ui"}
        for seat in range(5):
            service = config["services"][f"runtime-seat-{seat}"]
            assert set(service["networks"]) == {"runtime"}
            assert not service.get("ports") and not service.get("env_file")
            assert len(service["volumes"]) == 1 and service["volumes"][0]["read_only"]
            assert service["volumes"][0]["source"].endswith(f"seat-{seat}")
        provider = subprocess.run([*command, "-f", "docker-compose.runtime-provider.yml", "config", "--format", "json"],
            cwd=ROOT, env={**env, "OPENROUTER_API_KEY": "phase6-test-key"}, capture_output=True, text=True, timeout=30)
        assert provider.returncode == 0, provider.stderr
        provider_config = json.loads(provider.stdout)
        assert "OPENROUTER_API_KEY" not in provider_config["services"]["viewer"]["environment"]
        for seat in range(5):
            assert provider_config["services"][f"runtime-seat-{seat}"]["environment"]["OPENROUTER_API_KEY"] == "phase6-test-key"
        compose("up", "--build", "--detach", "--wait", "viewer", timeout=1500)
        port = compose("port", "viewer", "8080").strip()
        with urlopen(f"http://{port}/api/health", timeout=15) as response:
            assert json.load(response)["ok"]
        compose("exec", "-T", "viewer", "a2a-run", "experiments/all_games_runtime_smoke.yaml", "--smoke-test",
                "--smoke-episodes-per-cell", "2", "--max-parallelism", "8", "--results-dir", "/data/cli-results", timeout=180)
        compose("exec", "-T", "viewer", "python", "tests/runtime/launch_checks.py", timeout=240)
        for seat in range(5):
            compose("exec", "-T", f"runtime-seat-{seat}", "python", "-c",
                    "import os; from pathlib import Path; assert not Path('/data').exists(); "
                    "assert not Path('/workspace').exists(); assert not list(Path('/provision').glob('*.json')); "
                    "assert not any(k.endswith('API_KEY') for k in os.environ)")
        assert all(secret not in compose("logs", "--no-color") for secret in ("join_ticket", "seat_secret", "lifecycle_credential", "Bearer "))
    finally:
        compose("down", "--volumes", "--remove-orphans")
        assert not compose("ps", "--all", "--quiet").strip()
