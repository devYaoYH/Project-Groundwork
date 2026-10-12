"""A newly generated environment must run before its author changes any rules."""
import os
from pathlib import Path
import subprocess
import sys
import tomllib

from a2a_engine.storage.sqlite import SQLiteEpisodeStore


def test_fresh_scaffold_imports_tests_and_persists_scripted_cli_results(tmp_path):
    repository = Path(__file__).resolve().parents[2]
    subprocess.run([sys.executable, str(repository / "scripts/new_game.py"), "onboarding-check",
                    "--games-dir", str(tmp_path / "games")], check=True, capture_output=True, text=True)
    package = tmp_path / "games/onboarding-check"
    metadata = tomllib.loads((package / "pyproject.toml").read_text())
    assert metadata["project"]["entry-points"]["a2a_engine.environments"] == {"onboarding_check": "onboarding_check"}
    assert (package / "onboarding_check/environment.py").is_file()
    environment = {key: value for key, value in os.environ.items()
                   if not (key.endswith("_API_KEY") or key.startswith(("LANGFUSE_", "OTEL_")))}
    environment["PYTHONPATH"] = str(package) + os.pathsep + environment.get("PYTHONPATH", "")
    environment["OTEL_TRACES_EXPORTER"] = "none"
    generated_tests = subprocess.run([sys.executable, "-m", "pytest", str(package / "tests"), "-q"],
                                    cwd=tmp_path, env=environment, check=True, capture_output=True, text=True, timeout=30)
    assert "4 passed" in generated_tests.stdout
    database = tmp_path / "results/scaffold.db"
    results = tmp_path / "results/artifacts"
    smoke = subprocess.run([sys.executable, str(package / "run.py"), str(package / "experiments/example.yaml"),
                            "--smoke-test", "--max-parallelism", "1", "--storage-path", str(database),
                            "--results-dir", str(results)], cwd=tmp_path, env=environment,
                           check=True, capture_output=True, text=True, timeout=30)
    assert "PASS  2/2 runs persisted and read back" in smoke.stdout
    traces = list(SQLiteEpisodeStore(path=database, results_dir=results).iter_episodes())
    assert len(traces) == 2
    assert all(trace.config.environment_id == "onboarding_check" for trace in traces)
    assert all(trace.metrics == {"turns_used": 6, "messages": 12} for trace in traces)
    assert all(len([event for event in trace.events if event.type == "message"]) == 12 for trace in traces)
