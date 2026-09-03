#!/usr/bin/env python3
"""Replay declared derived metrics from a local SQLite trace corpus.

Usage:
    python scripts/materialize_metrics.py --database results/a2a.db
"""

from __future__ import annotations

import argparse
from pathlib import Path

from a2a_engine.derived_metrics import materialize_derived_metrics
from a2a_engine.registry import discover_environments
from a2a_engine.storage.sqlite import SQLiteEpisodeStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Materialize derived release metrics from completed episodes.")
    parser.add_argument("--database", default="./results/a2a.db")
    parser.add_argument("--environment", help="Optional environment-name filter")
    args = parser.parse_args(argv)

    discover_environments()
    results = materialize_derived_metrics(
        SQLiteEpisodeStore(path=Path(args.database)), environment_id=args.environment,
    )
    changed = sum(item.changed for item in results)
    skipped = [item for item in results if item.skipped_reason]
    print(f"materialized={len(results) - len(skipped)} changed={changed} skipped={len(skipped)}")
    for item in skipped:
        print(f"  {item.episode_uid}: {item.skipped_reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
