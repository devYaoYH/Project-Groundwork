"""Scheduling must conserve coverage; image reuse must retain real execution."""

import subprocess
from collections import Counter
from pathlib import Path

import pytest
import yaml
from ci_sharding import assign_files

from tests.runtime.compose_support import run_compose


def test_shards_cover_new_and_existing_files_once_regardless_of_collection_order():
    counts = Counter({"slow.py": 20, "fast.py": 3, "new.py": 8, "other.py": 2})
    weights = {"slow.py": 30, "fast.py": 2, "removed.py": 1000}
    assigned, loads = assign_files(counts, weights, 3)
    reversed_counts = dict(reversed(list(counts.items())))
    assert assign_files(reversed_counts, weights, 3) == (assigned, loads)
    shards = [{path for path in counts if assigned[path] == index} for index in range(3)]
    assert set.union(*shards) == set(counts)
    assert sum(len(shard) for shard in shards) == len(counts)
    assert all(shards)
    assert assigned["slow.py"] != assigned["new.py"]


@pytest.mark.parametrize("prebuilt", [False, True])
def test_cached_bases_do_not_skip_fixture_builds_execution_or_teardown(monkeypatch, prebuilt):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("tests.runtime.compose_support.subprocess.run", run)
    options = {
        "env": {"GROUNDWORK_CI_PREBUILT_IMAGES": "1"} if prebuilt else {},
        "cwd": Path.cwd(),
        "timeout": 10,
    }
    command = ["docker", "compose"]
    run_compose(command, ["build", "runtime-environment", "runtime-agent"], **options)
    assert bool(calls) is not prebuilt
    calls.clear()
    run_compose(
        command, ["build", "runtime-environment", "runtime-agent"], fixture_agent=True, **options
    )
    assert calls[-1] == [
        *command,
        "build",
        *([] if prebuilt else ["runtime-environment"]),
        "runtime-agent",
    ]
    run_compose(command, ["up", "--build", "-d", "--wait", "viewer"], **options)
    assert ("--no-build" in calls[-1]) is prebuilt
    assert ("--build" in calls[-1]) is not prebuilt
    for operation in (["exec", "viewer", "python", "checks.py"], ["down", "--volumes"]):
        run_compose(command, operation, **options)
        assert calls[-1] == [*command, *operation]


def test_required_gates_include_every_shard_and_platform_stack():
    root = Path(__file__).resolve().parents[3]
    ci = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"]
    platform = yaml.safe_load((root / ".github/workflows/platform.yml").read_text())["jobs"]
    assert set(ci["test"]["needs"]) == {"python-tests", "package-tests"}
    assert ci["runtime-containers"]["needs"] == "runtime-tests"
    assert set(platform["validate"]["needs"]) == {"platform-tests", "platform-stack"}
    assert set(platform["deploy"]["needs"]) == {"validate", "platform-stack"}
    for job in (ci["test"], ci["runtime-containers"], platform["validate"]):
        assert job["if"] == "always()"
        assert "= success" in job["steps"][0]["run"]


def test_workflow_shard_totals_follow_the_single_matrix_dimension():
    root = Path(__file__).resolve().parents[3]
    jobs = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())["jobs"]
    for name in ("python-tests", "runtime-tests"):
        matrix = jobs[name]["strategy"]["matrix"]
        assert set(matrix) == {"shard"}
        assert matrix["shard"] == list(range(1, len(matrix["shard"]) + 1))
        commands = "\n".join(step.get("run", "") for step in jobs[name]["steps"])
        assert "--ci-shard=${{ matrix.shard }}/${{ strategy.job-total }}" in commands
