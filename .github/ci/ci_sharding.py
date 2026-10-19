"""Pytest plugin: deterministic, duration-balanced shards that keep files intact.

Usage: PYTHONPATH=.github/ci pytest -p ci_sharding --ci-shard=1/4 ...
Weights affect scheduling only; every collected file, including new files,
belongs to exactly one shard. Separate runners isolate repository mutations
and mutable Docker tags without weakening protocol or cleanup coverage.
"""

import json
from collections import Counter
from pathlib import Path

import pytest


def assign_files(counts, weights, shards):
    if shards < 1:
        raise ValueError("Shard count must be positive")
    costs = {path: float(weights.get(path, max(1, count * 0.1))) for path, count in counts.items()}
    loads = [0.0] * shards
    assigned = {}
    for path in sorted(counts, key=lambda path: (-costs[path], path)):
        target = min(range(shards), key=lambda index: (loads[index], index))
        assigned[path] = target
        loads[target] += costs[path]
    return assigned, loads


def pytest_addoption(parser):
    parser.addoption("--ci-shard", help="One-based shard index/count, e.g. 1/4")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    option = config.getoption("--ci-shard")
    if not option:
        return
    try:
        index, total = map(int, option.split("/"))
        if not 1 <= index <= total:
            raise ValueError
    except ValueError as error:
        raise pytest.UsageError(
            "--ci-shard must be index/count with 1 <= index <= count"
        ) from error
    root = Path(config.rootpath)
    weights = json.loads(Path(__file__).with_name("ci_durations.json").read_text())
    paths = {item: item.path.relative_to(root).as_posix() for item in items}
    assigned, loads = assign_files(Counter(paths.values()), weights, total)
    selected = [item for item in items if assigned[paths[item]] == index - 1]
    deselected = [item for item in items if assigned[paths[item]] != index - 1]
    config.hook.pytest_deselected(items=deselected)
    items[:] = selected
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter:
        reporter.write_line(
            f"CI shard {index}/{total}: {len(selected)} tests; estimated file loads {loads}"
        )
