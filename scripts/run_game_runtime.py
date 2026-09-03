#!/usr/bin/env python3
"""Execute one reviewed environment runtime release locally or inside its image.

Each release lives in ``games/<environment>/runtime/release.json``.  The CLI validates
that the experiment names the release's environment before delegating to the ordinary
runner, preserving the identical trace/manifest and Redis event-stream path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from a2a_engine.experiment import load_experiment
from a2a_engine.registry import get_environment_spec
from a2a_engine.storage.sqlite import SQLiteEpisodeStore
from expt_runner.run_experiment import main as run_experiment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, help="runtime/release.json")
    parser.add_argument("--experiment", help="override the release's experiment path")
    parser.add_argument("--storage-path", default="/data/a2a_traces.db")
    parser.add_argument("--max-parallelism", type=int, default=1)
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    release_path = Path(args.release).resolve()
    release = json.loads(release_path.read_text(encoding="utf-8"))
    environment_id = str(release["environment_id"])
    experiment_path = Path(args.experiment or release["experiment"])
    if not experiment_path.is_absolute():
        experiment_path = (release_path.parents[3] / experiment_path).resolve()
    experiment = load_experiment(experiment_path)
    games = experiment.environment_ids()
    if games != [environment_id]:
        raise ValueError(f"release {release_path} is for {environment_id!r}, experiment selects {games!r}")
    runner_args = [str(experiment_path), "--storage-path", args.storage_path,
                   "--max-parallelism", str(args.max_parallelism)]
    if args.smoke_test:
        runner_args.append("--smoke-test")
    if args.dry_run:
        runner_args.append("--dry-run")
    result = run_experiment(runner_args)
    if result != 0:
        return result

    declaration = get_environment_spec(environment_id).declaration
    if declaration is None:
        raise ValueError(f"environment {environment_id!r} does not publish a release declaration")
    declared = {
        measure.name
        for measure in declaration.measures
        if measure.producer == "environment"
    }
    if not declared:
        return result
    emitted = [
        episode.metrics
        for episode in SQLiteEpisodeStore(path=args.storage_path).iter_episodes()
        if episode.config.environment_id == environment_id
    ]
    if not emitted:
        raise AssertionError(f"runtime release for {environment_id!r} emitted no episodes")
    missing = {
        name
        for name in declared
        if any(name not in metrics for metrics in emitted)
    }
    if missing:
        raise AssertionError(
            f"release declaration names environment measures absent from emitted metrics: {sorted(missing)}"
        )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
