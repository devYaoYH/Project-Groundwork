#!/usr/bin/env python3
"""Ingest completed Calendar VPS analysis into a local trace store.

This command is intentionally post-hoc: it reads a CSV produced by an analysis
job, joins it only to completed episodes in SQLite, and writes versioned derived
artifacts. The Calendar leaderboard can then be rebuilt without contacting a
model, cloud provider, or running environment session.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from a2a_engine.ratings import rebuild_rating_snapshot
from a2a_engine.storage.sqlite import SQLiteEpisodeStore
from calendar_game.artifacts import ingest_calendar_vps_by_game
from calendar_game.ratings import (
    CalendarRatingAdapter,
    load_target_vps_from_game_target_csv,
    load_target_vps_from_pair_csv,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="CSV produced by a Calendar VPS analysis")
    parser.add_argument("--database", type=Path, required=True, help="SQLite trace store")
    parser.add_argument(
        "--input-kind",
        choices=("environment-target", "pair"),
        default="environment-target",
        help="CSV shape: environment-target summary (default) or pair-level VPS rows",
    )
    parser.add_argument("--value-column", help="Override the VPS value column")
    parser.add_argument("--weight-mode", default="cost", help="Pair CSV weight mode")
    parser.add_argument("--include-nonparticipants", action="store_true")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild the Calendar snapshot after ingestion")
    args = parser.parse_args()

    if args.input_kind == "environment-target":
        scores = load_target_vps_from_game_target_csv(
            args.input,
            **({"value_column": args.value_column} if args.value_column else {}),
        )
    else:
        scores = load_target_vps_from_pair_csv(
            args.input,
            weight_mode=args.weight_mode,
            participants_only=not args.include_nonparticipants,
            **({"value_column": args.value_column} if args.value_column else {}),
        )

    store = SQLiteEpisodeStore(path=args.database)
    result = ingest_calendar_vps_by_game(
        store,
        scores,
        metadata={
            "source": "calendar-vps-csv",
            "source_path": str(args.input.resolve()),
            "input_kind": args.input_kind,
        },
    )
    output: dict[str, object] = {
        "written_episode_uids": result.written_episode_uids,
        "unchanged_episode_uids": result.unchanged_episode_uids,
        "missing_trace_episode_uids": result.missing_trace_episode_uids,
        "non_calendar_episode_uids": result.non_calendar_episode_uids,
    }
    if args.rebuild:
        materialization = rebuild_rating_snapshot(store, CalendarRatingAdapter())
        output["rating_event_count"] = len(materialization.events)
        output["suppressed_metric_names"] = materialization.suppressed_metric_names
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
