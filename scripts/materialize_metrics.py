#!/usr/bin/env python3
"""Replay declared derived metrics from a local SQLite trace corpus.

Usage:
    python scripts/materialize_metrics.py --database results/a2a.db
"""

from __future__ import annotations

import argparse
from pathlib import Path

from a2a_engine.derived_metrics import materialize_derived_metrics
from a2a_engine.registry import discover_games
from a2a_engine.storage.sqlite import SQLiteTraceStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Materialize derived environment metrics from completed traces.")
    parser.add_argument("--database", default="./results/a2a_traces.db")
    parser.add_argument("--game", help="Optional game-name filter")
    args = parser.parse_args(argv)

    discover_games()
    results = materialize_derived_metrics(
        SQLiteTraceStore(path=Path(args.database)), game_name=args.game,
    )
    changed = sum(item.changed for item in results)
    skipped = [item for item in results if item.skipped_reason]
    print(f"materialized={len(results) - len(skipped)} changed={changed} skipped={len(skipped)}")
    for item in skipped:
        print(f"  {item.game_id}: {item.skipped_reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
