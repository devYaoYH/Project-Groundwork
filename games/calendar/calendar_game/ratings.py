"""Calendar's public rating API, implemented in the shared read layer."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from a2a_engine.ratings.calendar import (  # noqa: F401
    CALENDAR_RATING_ADAPTER_VERSION,
    CALENDAR_RATING_METRICS,
    CALENDAR_RATING_VARIANT_METRICS,
    CALENDAR_SCORE_MARGIN_RATING_METRICS,
    CALENDAR_VPS_ARTIFACT_KIND,
    CALENDAR_VPS_ARTIFACT_VERSION,
    SCORE_MARGIN_RATING_VARIANT,
    CalendarLegacyScoreMarginMetricExtractor,
    CalendarRatingAdapter,
    CalendarScoreMarginMetricExtractor,
    extract_calendar_rating_event,
    load_task_scenario_for_trace,
    make_calendar_vps_artifact,
)


def load_calendar_trace(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_target_vps_from_pair_csv(
    path: str | Path,
    *,
    weight_mode: str | None = "cost",
    participants_only: bool = True,
    value_column: str = "vps_loss",
) -> dict[str, dict[int, float]]:
    """Group pair-round VPS rows by leaked-about target agent."""
    grouped: dict[str, dict[int, float]] = {}
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if weight_mode and (row.get("weight_mode") or "").strip().lower() != weight_mode:
                continue
            if participants_only:
                if str(row.get("target_is_participant")).lower() != "true":
                    continue
                if str(row.get("observer_is_participant")).lower() != "true":
                    continue
            episode_uid = row.get("episode_uid")
            if not episode_uid:
                continue
            try:
                target_agent = int(row["target_agent"])
                value = float(row[value_column])
            except (KeyError, TypeError, ValueError):
                continue
            grouped.setdefault(episode_uid, {})
            grouped[episode_uid][target_agent] = grouped[episode_uid].get(target_agent, 0.0) + value
    return grouped


def load_target_vps_from_game_target_csv(
    path: str | Path,
    *,
    value_column: str = "excess_calibrated_vps_loss_total",
) -> dict[str, dict[int, float]]:
    """Load {episode_uid: {target_agent: vps_loss}} from a game-target CSV."""
    grouped: dict[str, dict[int, float]] = {}
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            episode_uid = row.get("episode_uid")
            if not episode_uid:
                continue
            try:
                target_agent = int(row["target_agent"])
                value = float(row[value_column])
            except (KeyError, TypeError, ValueError):
                continue
            grouped.setdefault(episode_uid, {})
            grouped[episode_uid][target_agent] = max(0.0, value)
    return grouped
