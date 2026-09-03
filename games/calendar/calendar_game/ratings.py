"""Calendar-specific adapter for environment-agnostic OpenSkill ratings."""

from __future__ import annotations

import csv
from copy import deepcopy
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from a2a_engine.derived import DerivedArtifact, trace_digest
from a2a_engine.derived_metrics import (
    DerivedMetricResult,
    register_derived_metric_extractor,
)
from a2a_engine.ratings import MetricSpec, RatingEvent, RatingParticipant


CALENDAR_RATING_METRICS = [
    MetricSpec(name="coordination_ratio", higher_is_better=True, weight=0.40),
    MetricSpec(name="excess_cost", higher_is_better=False, weight=0.35),
    MetricSpec(name="excess_vps", higher_is_better=False, weight=0.25),
]

SCORE_MARGIN_RATING_VARIANT = "score_margin_v1"

CALENDAR_SCORE_MARGIN_RATING_METRICS = [
    MetricSpec(name="coordination_ratio", higher_is_better=True, weight=0.40, tie_tolerance=0.2),
    MetricSpec(name="excess_cost", higher_is_better=False, weight=0.35, tie_tolerance=1.5),
    MetricSpec(name="excess_vps", higher_is_better=False, weight=0.25, tie_tolerance=1.0),
]

CALENDAR_RATING_VARIANT_METRICS = {
    "default": CALENDAR_RATING_METRICS,
    SCORE_MARGIN_RATING_VARIANT: CALENDAR_SCORE_MARGIN_RATING_METRICS,
}

CALENDAR_RATING_ADAPTER_VERSION = "calendar-rating-v1"
CALENDAR_VPS_ARTIFACT_KIND = "calendar.vps"
CALENDAR_VPS_ARTIFACT_VERSION = "v1"


def _as_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _started_at(trace: dict[str, Any]) -> datetime:
    raw = trace.get("started_at") or trace.get("ended_at")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _agent_model(trace: dict[str, Any], agent_id: int) -> str:
    """Rating identity for an agent.

    ``rating_player_id`` wins over ``model`` when present. Two agents can run the
    *same* model under different prompt variants — a redteam variant against the
    baseline, say — and for OpenSkill those must be distinct players. Keying on
    the model name alone would silently pool their ratings into one, which makes
    the comparison the experiment was built to measure impossible to read.
    """
    agents = (trace.get("config") or {}).get("agents") or []
    if agent_id < len(agents):
        spec = agents[agent_id] or {}
        return str(
            spec.get("rating_player_id")
            or spec.get("model")
            or spec.get("type")
            or f"agent_{agent_id}"
        )
    final_state = trace.get("final_state") or {}
    models = final_state.get("agent_models") or []
    if agent_id < len(models):
        return str(models[agent_id])
    return f"agent_{agent_id}"


def _num_agents(trace: dict[str, Any]) -> int:
    config = trace.get("config") or {}
    metrics = trace.get("metrics") or {}
    return int(config.get("num_agents") or metrics.get("num_agents") or len(config.get("agents") or []))


def _contribution_by_agent(trace: dict[str, Any]) -> dict[int, dict[str, Any]]:
    rows = (trace.get("final_state") or {}).get("contribution_scores") or []
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        try:
            agent_id = int(row.get("agent_id"))
        except (TypeError, ValueError):
            continue
        out[agent_id] = row
    return out


def _list_metric(trace: dict[str, Any], *names: str) -> list[Any]:
    metrics = trace.get("metrics") or {}
    final_state = trace.get("final_state") or {}
    for name in names:
        value = metrics.get(name)
        if isinstance(value, list):
            return value
        value = final_state.get(name)
        if isinstance(value, list):
            return value
    return []


def _task_setting(trace: dict[str, Any], task_scenario: dict[str, Any] | None = None) -> str:
    config = trace.get("config") or {}
    metrics = trace.get("metrics") or {}
    haystack = " ".join(
        str(value or "").lower()
        for value in (
            config.get("task_path"),
            metrics.get("task_path"),
            config.get("experiment_name"),
            config.get("task_id"),
            metrics.get("task_id"),
        )
    )
    if "varied" in haystack:
        return "varied"
    if "uniform" in haystack:
        return "uniform"
    if task_scenario:
        for calendar in task_scenario.get("calendars") or []:
            for slot in calendar:
                if isinstance(slot, dict) and int(slot.get("cost", 1)) not in {0, 1}:
                    return "varied"
    return "uniform"


def _repo_calendar_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _task_path_candidates(raw_path: object, repo_root: str | Path | None = None) -> list[Path]:
    if not raw_path:
        return []
    path = Path(str(raw_path))
    candidates = [path]
    roots = []
    if repo_root is not None:
        roots.append(Path(repo_root))
    roots.extend([Path.cwd(), _repo_calendar_root(), _repo_calendar_root().parents[1]])
    for root in roots:
        candidates.append(root / path)
        candidates.append(root / "games" / "calendar" / path)
    out: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate if candidate.is_absolute() else candidate.resolve()
        if resolved not in seen:
            out.append(resolved)
            seen.add(resolved)
    return out


def load_task_scenario_for_trace(
    trace: dict[str, Any],
    *,
    repo_root: str | Path | None = None,
) -> dict[str, Any] | None:
    """Load the task scenario referenced by a trace, when the local task file is available."""

    final_state = trace.get("final_state") or {}
    rating_context = final_state.get("rating_context") or {}
    if isinstance(rating_context, dict) and rating_context.get("calendars") is not None:
        # New episodes carry their rating inputs, so a copied trace remains
        # analyzable without a checkout-local task file.
        return rating_context

    config = trace.get("config") or {}
    metrics = trace.get("metrics") or {}
    task_id = config.get("task_id") or metrics.get("task_id")
    if not task_id:
        return None
    task_path = config.get("task_path") or metrics.get("task_path")
    for path in _task_path_candidates(task_path, repo_root=repo_root):
        if not path.exists():
            continue
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                if str(row.get("task_id")) == str(task_id):
                    return row
    return None


def _errand_unit(cost: object, setting: str) -> float:
    if setting == "varied":
        return {1: 1.0, 10: 2.0, 100: 3.0}.get(int(cost), float(cost))
    return 1.0


def _errand_cost_by_id(task_scenario: dict[str, Any] | None, trace: dict[str, Any]) -> dict[int, float]:
    lookup: dict[int, float] = {}
    calendar_sources = []
    if task_scenario:
        calendar_sources.append(task_scenario.get("calendars") or [])
    calendar_sources.append((trace.get("final_state") or {}).get("calendars") or [])
    for calendars in calendar_sources:
        for calendar in calendars:
            for slot in calendar or []:
                if not isinstance(slot, dict) or "errand_id" not in slot:
                    continue
                try:
                    lookup[int(slot["errand_id"])] = float(slot.get("cost", 1))
                except (TypeError, ValueError):
                    continue
    return lookup


def _realized_errand_units_by_agent(
    trace: dict[str, Any],
    *,
    task_scenario: dict[str, Any] | None,
    setting: str,
) -> list[float]:
    num_agents = _num_agents(trace)
    totals = [0.0 for _ in range(num_agents)]
    errand_costs = _errand_cost_by_id(task_scenario, trace)
    for event in trace.get("events") or []:
        if event.get("type") != "cell_applied":
            continue
        data = event.get("data") or {}
        try:
            agent_id = int(data.get("agent_id"))
        except (TypeError, ValueError):
            continue
        if agent_id < 0 or agent_id >= num_agents:
            continue
        for action in data.get("actions") or []:
            if not isinstance(action, dict) or action.get("type") != "reschedule":
                continue
            try:
                item_id = int(action.get("item_id"))
            except (TypeError, ValueError):
                continue
            if item_id in errand_costs:
                totals[agent_id] += _errand_unit(errand_costs[item_id], setting)
    return totals


def _oracle_errand_units_by_agent(
    trace: dict[str, Any],
    *,
    task_scenario: dict[str, Any] | None,
    setting: str,
) -> list[float] | None:
    if not task_scenario:
        return None
    num_agents = _num_agents(trace)
    calendars = task_scenario.get("calendars") or []
    meetings = task_scenario.get("meetings") or []
    assignments = (trace.get("metrics") or {}).get("scheduled_only_oracle_assignments") or {}
    try:
        normalized_assignments = {int(meeting_id): int(slot) for meeting_id, slot in assignments.items()}
    except (TypeError, ValueError):
        return None
    if not normalized_assignments:
        return [0.0 for _ in range(num_agents)]

    working = deepcopy(calendars)
    totals = [0.0 for _ in range(num_agents)]
    for meeting in meetings:
        try:
            meeting_id = int(meeting["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if meeting_id not in normalized_assignments:
            continue
        slot = normalized_assignments[meeting_id]
        for agent_id in meeting.get("participants") or []:
            try:
                agent_idx = int(agent_id)
            except (TypeError, ValueError):
                continue
            if agent_idx < 0 or agent_idx >= len(working) or slot < 0 or slot >= len(working[agent_idx]):
                continue
            value = working[agent_idx][slot]
            if isinstance(value, dict):
                if "errand_id" in value and not value.get("blocked"):
                    totals[agent_idx] += _errand_unit(value.get("cost", 1), setting)
                target = next((i for i, v in enumerate(working[agent_idx]) if i != slot and v is None), None)
                if target is not None:
                    working[agent_idx][target] = value
            working[agent_idx][slot] = {"meeting_id": meeting_id, "cost": meeting.get("cost", 1)}
    return totals


def _score_margin_excess_cost_by_agent(
    trace: dict[str, Any],
    *,
    task_scenario: dict[str, Any] | None,
    setting: str,
) -> list[float] | None:
    oracle = _oracle_errand_units_by_agent(trace, task_scenario=task_scenario, setting=setting)
    if oracle is None:
        return None
    realized = _realized_errand_units_by_agent(trace, task_scenario=task_scenario, setting=setting)
    cap = 10.0 if setting == "varied" else 5.0
    return [
        min(cap, max(0.0, realized[i] - oracle[i]))
        for i in range(min(len(realized), len(oracle)))
    ]


def extract_calendar_rating_event(
    trace: dict[str, Any],
    *,
    source_path: str | None = None,
    excess_vps_by_agent: dict[int, float] | None = None,
    rating_variant: str = "default",
    task_scenario: dict[str, Any] | None = None,
    repo_root: str | Path | None = None,
) -> RatingEvent | None:
    """Extract one seat-level FFA rating event from a calendar trace."""

    num_agents = _num_agents(trace)
    if num_agents <= 1:
        return None

    participants = [
        RatingParticipant(
            participant_id=f"agent_{agent_id}",
            player_id=_agent_model(trace, agent_id),
            metadata={"agent_id": agent_id},
        )
        for agent_id in range(num_agents)
    ]
    contributions = _contribution_by_agent(trace)
    per_agent_excess = _list_metric(
        trace,
        "per_agent_scheduled_only_excess_burden",
        "per_agent_excess_burden",
        "per_agent_cost",
    )
    raw_per_agent_excess = list(per_agent_excess)
    variant_metadata: dict[str, Any] = {}
    if rating_variant == SCORE_MARGIN_RATING_VARIANT:
        task_scenario = task_scenario or load_task_scenario_for_trace(trace, repo_root=repo_root)
        setting = _task_setting(trace, task_scenario)
        score_margin_excess = _score_margin_excess_cost_by_agent(
            trace,
            task_scenario=task_scenario,
            setting=setting,
        )
        if score_margin_excess is not None:
            per_agent_excess = score_margin_excess
        variant_metadata = {
            "rating_variant": rating_variant,
            "score_margin_setting": setting,
            "score_margin_excess_cost_cap": 10.0 if setting == "varied" else 5.0,
            "metric_tie_tolerances": {
                "coordination_ratio": 0.2,
                "excess_cost": 2.0 if setting == "varied" else 1.0,
                "excess_vps": 1.0,
            },
        }

    metric_scores: dict[str, dict[str, float]] = {
        "coordination_ratio": {},
        "excess_cost": {},
    }
    raw_metric_scores: dict[str, dict[str, float]] = {
        "coordination_ratio": {},
        "excess_cost": {},
    }
    for agent_id in range(num_agents):
        participant_id = f"agent_{agent_id}"
        contribution = contributions.get(agent_id, {})
        coordination = _as_float(contribution.get("coordination_rate"))
        if coordination is None:
            coordination = _as_float((trace.get("metrics") or {}).get("coordination_rate"))
        if coordination is not None:
            metric_scores["coordination_ratio"][participant_id] = coordination
            raw_metric_scores["coordination_ratio"][participant_id] = coordination
        if agent_id < len(per_agent_excess):
            excess_cost = _as_float(per_agent_excess[agent_id])
            if excess_cost is not None:
                metric_scores["excess_cost"][participant_id] = max(0.0, excess_cost)
        if agent_id < len(raw_per_agent_excess):
            raw_excess_cost = _as_float(raw_per_agent_excess[agent_id])
            if raw_excess_cost is not None:
                raw_metric_scores["excess_cost"][participant_id] = max(0.0, raw_excess_cost)

    if excess_vps_by_agent is not None:
        metric_scores["excess_vps"] = {}
        raw_metric_scores["excess_vps"] = {}
        for agent_id in range(num_agents):
            value = excess_vps_by_agent.get(agent_id, 0.0)
            if math.isfinite(float(value)):
                metric_scores["excess_vps"][f"agent_{agent_id}"] = max(0.0, float(value))
                raw_metric_scores["excess_vps"][f"agent_{agent_id}"] = max(0.0, float(value))

    metric_scores = {
        name: scores
        for name, scores in metric_scores.items()
        if len(scores) >= 2
    }
    raw_metric_scores = {
        name: scores
        for name, scores in raw_metric_scores.items()
        if len(scores) >= 2
    }
    if not metric_scores:
        return None

    config = trace.get("config") or {}
    return RatingEvent(
        episode_uid=str(trace.get("episode_uid") or Path(source_path or "").stem),
        environment_id=str(config.get("environment_id") or "calendar"),
        participants=participants,
        metric_scores=metric_scores,
        timestamp=_started_at(trace),
        source_path=source_path,
        metadata={
            "experiment_name": config.get("experiment_name"),
            "episode_id": config.get("episode_id"),
            "task_id": config.get("task_id") or (trace.get("metrics") or {}).get("task_id"),
            "raw_metric_scores": raw_metric_scores,
            **variant_metadata,
        },
    )


def make_calendar_vps_artifact(
    trace: dict[str, Any],
    excess_vps_by_agent: Mapping[int | str, float],
    *,
    version: str = CALENDAR_VPS_ARTIFACT_VERSION,
    metadata: Mapping[str, Any] | None = None,
) -> DerivedArtifact:
    """Package a completed post-hoc VPS analysis for engine-owned storage.

    VPS is not a live-environment measurement. Storing it as a trace-digest-bound
    artifact prevents a rating job from accidentally joining results from a
    different task revision or treating missing privacy analysis as zero.
    """
    normalized: dict[str, float] = {}
    for agent_id, value in excess_vps_by_agent.items():
        number = _as_float(value)
        if number is not None:
            normalized[str(agent_id)] = max(0.0, number)
    return DerivedArtifact(
        episode_uid=str(trace.get("episode_uid") or ""),
        kind=CALENDAR_VPS_ARTIFACT_KIND,
        version=version,
        trace_digest=trace_digest(trace),
        payload={"excess_vps_by_agent": normalized},
        metadata=dict(metadata or {}),
    )


class CalendarRatingAdapter:
    """Engine registration adapter for completed Calendar episodes only."""

    environment_id = "calendar"

    def __init__(self, rating_variant: str = "default") -> None:
        if rating_variant not in CALENDAR_RATING_VARIANT_METRICS:
            raise ValueError(f"Unknown Calendar rating variant: {rating_variant!r}")
        self.rating_variant = rating_variant
        # Adapter version, not wall-clock time, is the idempotency namespace
        # for materialized events. A metric-definition change must use a new
        # adapter version so historical projections remain auditable.
        suffix = "" if rating_variant == "default" else f"-{rating_variant}"
        self.version = f"{CALENDAR_RATING_ADAPTER_VERSION}{suffix}"
        self.metrics = CALENDAR_RATING_VARIANT_METRICS[rating_variant]

    def extract(
        self,
        trace: dict[str, Any],
        artifacts: Mapping[str, DerivedArtifact],
    ) -> RatingEvent | None:
        artifact = artifacts.get(
            f"{CALENDAR_VPS_ARTIFACT_KIND}@{CALENDAR_VPS_ARTIFACT_VERSION}"
        )
        values = None
        if artifact is not None:
            raw = artifact.payload.get("excess_vps_by_agent")
            if isinstance(raw, dict):
                values = {
                    int(agent_id): value
                    for agent_id, value in raw.items()
                    if str(agent_id).lstrip("-").isdigit()
                }
        task_scenario = None
        if self.rating_variant == SCORE_MARGIN_RATING_VARIANT:
            task_scenario = load_task_scenario_for_trace(trace)
        return extract_calendar_rating_event(
            trace,
            excess_vps_by_agent=values,
            rating_variant=self.rating_variant,
            task_scenario=task_scenario,
        )


class CalendarScoreMarginMetricExtractor:
    """Post-hoc participant metric declared by Calendar's typed release."""

    identifier = "calendar.score_margin"
    version = "v1"

    def extract(
        self,
        trace: dict[str, Any],
        artifacts: Mapping[str, DerivedArtifact],
    ) -> DerivedMetricResult | None:
        adapter = CalendarRatingAdapter(SCORE_MARGIN_RATING_VARIANT)
        event = adapter.extract(trace, artifacts)
        if event is None:
            return None
        values = event.metric_scores.get("excess_cost")
        if not values:
            return None
        return DerivedMetricResult(
            values={"excess_cost": values},
            metadata={
                "rating_variant": SCORE_MARGIN_RATING_VARIANT,
                "rating_adapter_version": adapter.version,
            },
        )


class CalendarLegacyScoreMarginMetricExtractor:
    """Read the pre-v1 Calendar declaration without changing its semantics.

    Earlier local episodes named a maximizing participant measurement
    ``score_margin`` and selected ``calendar.rating.v1``.  The v1 contract
    makes the underlying quantity explicit as minimizing ``excess_cost``.
    This adapter keeps old records replayable by expressing the same ordering
    as ``-excess_cost`` under their original, maximizing name.  It is a
    compatibility namespace only; new environments must use
    ``calendar.score_margin`` and ``excess_cost``.
    """

    identifier = "calendar.rating.v1"
    version = "legacy-v1"

    def extract(
        self,
        trace: dict[str, Any],
        artifacts: Mapping[str, DerivedArtifact],
    ) -> DerivedMetricResult | None:
        adapter = CalendarRatingAdapter(SCORE_MARGIN_RATING_VARIANT)
        event = adapter.extract(trace, artifacts)
        if event is None:
            return None
        excess_cost = event.metric_scores.get("excess_cost")
        if not excess_cost:
            return None
        return DerivedMetricResult(
            values={
                "score_margin": {
                    participant_id: -float(cost)
                    for participant_id, cost in excess_cost.items()
                }
            },
            metadata={
                "compatibility": "pre-v1 calendar.rating.v1",
                "score_margin_definition": "negative_score_margin_excess_cost",
                "rating_variant": SCORE_MARGIN_RATING_VARIANT,
                "rating_adapter_version": adapter.version,
            },
        )


register_derived_metric_extractor(CalendarScoreMarginMetricExtractor())
register_derived_metric_extractor(CalendarLegacyScoreMarginMetricExtractor())


def load_calendar_trace(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def load_target_vps_from_pair_csv(
    path: str | Path,
    *,
    weight_mode: str | None = "cost",
    participants_only: bool = True,
    value_column: str = "vps_loss",
) -> dict[str, dict[int, float]]:
    """Group pair-round VPS rows by leaked-about target agent.

    Returns {episode_uid: {target_agent: total_vps_loss}}. The target grouping
    measures how much information about that agent's calendar moved observers'
    beliefs.
    """

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
    """Load {episode_uid: {target_agent: vps_loss}} from a environment-target summary CSV."""

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
