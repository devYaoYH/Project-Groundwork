"""What counts as a result -- defined once, beside the store.

Every row in ``episodes`` is a record of something that ran. Only some of them
are *measurements*: a smoke launch plays scripted stand-in agents to prove the
pipeline, and its trace is a health check that happens to look exactly like a
result. Readers split cleanly into two kinds, and only the first filters:

``"what is the result for this slot?"``
    ``cell_evidence``, ``completed_episode_ids`` (``--resume``) and
    ``iter_episodes(results=True)`` (the rating pipeline and the calendar
    leaderboard). They apply :data:`RESULT_PREDICATE` in SQL, or
    :func:`counts_as_result` when the join happens in Python.

``"what happened in this launch / store?"``
    ``launch_detail``, ``progress``, settlement, and the episode browser. They
    stay unfiltered -- a smoke launch still settles ``COMPLETED`` from its own
    smoke rows -- but the mode is visible on every row.

The condition is a table rather than a string so that both spellings are
generated from one place. Phase 7 appends ``provenance_grade = 'verified'``
here; a future ``env`` column would too. Every reader routed through this
module picks the new condition up at once, and none of them has to be found.
"""

from __future__ import annotations

import json
import math
import statistics
from collections.abc import Iterable, Mapping
from types import MappingProxyType
from typing import Any

from a2a_engine.provenance import RUN_MODE_LIVE, promoted_columns
from a2a_engine.schemas import EpisodeTrace

#: Column -> the value a row must hold to count as a result.
RESULT_CONDITIONS: Mapping[str, str] = MappingProxyType({"run_mode": RUN_MODE_LIVE})

#: What an absent column means, mirroring each column's schema default. A row
#: or manifest written before a column existed claims the default, which is
#: exactly what the column itself would say after its additive migration.
_COLUMN_DEFAULTS: Mapping[str, str] = MappingProxyType({"run_mode": RUN_MODE_LIVE})


def result_predicate(alias: str | None = None) -> str:
    """The SQL spelling of "counts as a result", optionally table-qualified.

    Values are literals from :data:`RESULT_CONDITIONS`, never caller input, so
    interpolating them keeps the statement free of anything a request chose.
    """
    prefix = f"{alias}." if alias else ""
    return " AND ".join(
        f"{prefix}{column} = '{value}'" for column, value in RESULT_CONDITIONS.items()
    )


#: The unqualified predicate, for a statement over ``episodes`` alone.
RESULT_PREDICATE = result_predicate()


def counts_as_result(row: Any) -> bool:
    """The Python twin of :data:`RESULT_PREDICATE`.

    Accepts a column mapping (a ``dict``, a ``sqlite3.Row``, a manifest record)
    or an :class:`EpisodeTrace`, whose provenance block is the durable copy the
    promoted columns are projected from. Used wherever a result read is joined
    in memory rather than in one statement, so the two cannot drift apart.
    """
    fields = _result_fields(row)
    return all(fields.get(column) == value for column, value in RESULT_CONDITIONS.items())


def _result_fields(row: Any) -> dict[str, Any]:
    if isinstance(row, EpisodeTrace):
        promoted = promoted_columns(row)
        return {column: promoted.get(column) for column in RESULT_CONDITIONS}
    keys = set(row.keys()) if hasattr(row, "keys") else set()
    fields: dict[str, Any] = {}
    for column in RESULT_CONDITIONS:
        value = row[column] if column in keys else None
        fields[column] = _COLUMN_DEFAULTS.get(column) if value is None else value
    return fields


# --- one-time attribution of rows that predate ``run_mode`` ------------------

#: Experiments that only ever run with scripted stand-ins from the CLI.
#:
#: A row written before ``run_mode`` existed and not backed by any launch is a
#: direct ``a2a-run``. Nothing in such a row records whether ``--smoke-test``
#: was passed -- a smoke trace's config is indistinguishable from a live one --
#: so the backfill can only correct the rows whose *experiment* is smoke by
#: declaration. ``experiments/all_games_smoke.yaml`` is the Compose ``runner``
#: service's command and the largest body of such rows; it declares no agents
#: and exists only to be run with ``--smoke-test``. Every row written since the
#: column exists records its mode itself, so this list never needs to grow.
LEGACY_SMOKE_EXPERIMENTS = frozenset({"all_games_smoke"})


# --- the replication rollup, shared by the SQL and two-step reads -------------


def latest_result_attempts(rows: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Each episode's highest attempt among attempts that could be a result.

    ``rows`` carry ``episode_id``, ``attempt``, ``id`` and the launch's
    ``run_mode``. Ranking only result attempts is what stops a later smoke or
    dry run from replacing -- or, having no trace, hiding -- a live result.
    Ordered exactly as the SQL window: ``attempt DESC, id DESC``.
    """
    best: dict[str, tuple[tuple[int, str], Mapping[str, Any]]] = {}
    for row in rows:
        if not counts_as_result(row):
            continue
        rank = (int(row["attempt"]), str(row["id"]))
        current = best.get(row["episode_id"])
        if current is None or rank > current[0]:
            best[row["episode_id"]] = (rank, row)
    return {episode_id: row for episode_id, (_, row) in best.items()}


def latest_result_traces(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, int], Mapping[str, Any]]:
    """The newest result row per ``(episode_id, attempt)``.

    Ordered exactly as the SQL window: ``created_at DESC, episode_uid DESC``.
    A missing ``created_at`` sorts lowest, as SQLite sorts ``NULL``.
    """
    best: dict[tuple[str, int], tuple[tuple[str, str], Mapping[str, Any]]] = {}
    for row in rows:
        if not counts_as_result(row) or not row["episode_id"]:
            continue
        key = (str(row["episode_id"]), int(row["attempt"]))
        rank = (str(row["created_at"] or ""), str(row["episode_uid"]))
        current = best.get(key)
        if current is None or rank > current[0]:
            best[key] = (rank, row)
    return {key: row for key, (_, row) in best.items()}


def summarize_cell_evidence(
    cells: Iterable[Mapping[str, Any]],
    executions: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Roll each cell's latest result per replication into its evidence.

    ``cells`` carry ``cell_id``, ``levels``, ``episodes_planned`` and
    ``episode_configs`` as stored. ``executions`` carry ``cell_id``,
    ``episode_id``, the attempt's ``status``, and the matched trace's
    ``trace_status`` and ``metrics`` (either ``None`` when no result trace
    exists). Both read paths feed this one function, so their outputs can only
    differ by the rows they chose.
    """
    latest_by_episode = {
        (row["cell_id"], row["episode_id"]): row for row in executions
    }
    evidence: list[dict[str, Any]] = []
    for cell in cells:
        levels = _decoded(cell["levels"], {})
        configs = _decoded(cell["episode_configs"], [])
        planned_ids = {
            str(config.get("episode_id"))
            for config in (configs if isinstance(configs, list) else [])
            if isinstance(config, dict) and config.get("episode_id")
        }
        status_counts: dict[str, int] = {"NOT_STARTED": int(cell["episodes_planned"])}
        completed_replicas = 0
        metric_values: dict[str, dict[str, list[float] | list[bool]]] = {}

        for episode_id in planned_ids:
            execution = latest_by_episode.get((cell["cell_id"], episode_id))
            if execution is None:
                continue
            status_counts["NOT_STARTED"] -= 1
            status = str(execution["status"])
            status_counts[status] = status_counts.get(status, 0) + 1
            if status != "COMPLETED" or execution["trace_status"] != "COMPLETED":
                continue
            completed_replicas += 1
            metrics = _decoded(execution["metrics"] or "{}", {})
            if not isinstance(metrics, dict):
                continue
            for name, value in metrics.items():
                if isinstance(value, bool):
                    metric_values.setdefault(str(name), {"number": [], "boolean": []})["boolean"].append(value)
                elif isinstance(value, (int, float)) and math.isfinite(value):
                    metric_values.setdefault(str(name), {"number": [], "boolean": []})["number"].append(float(value))

        status_counts = {name: count for name, count in status_counts.items() if count}
        metric_summaries: list[dict[str, Any]] = []
        for name in sorted(metric_values):
            values = metric_values[name]
            numbers = values["number"]
            booleans = values["boolean"]
            # A metric whose native type changes between traces cannot be
            # compared safely, so leave it out rather than coerce it.
            if numbers and not booleans:
                summary: dict[str, Any] = {
                    "name": name,
                    "kind": "number",
                    "n": len(numbers),
                    "mean": statistics.fmean(numbers),
                    "min": min(numbers),
                    "max": max(numbers),
                }
                if len(numbers) > 1:
                    summary["stddev"] = statistics.stdev(numbers)
                metric_summaries.append(summary)
            elif booleans and not numbers:
                true_count = sum(booleans)
                metric_summaries.append({
                    "name": name,
                    "kind": "boolean",
                    "n": len(booleans),
                    "true_count": true_count,
                    "false_count": len(booleans) - true_count,
                })
        evidence.append({
            "cell_id": cell["cell_id"],
            "levels": levels if isinstance(levels, dict) else {},
            "planned_replicas": int(cell["episodes_planned"]),
            "completed_replicas": completed_replicas,
            "status_counts": status_counts,
            "metric_summaries": metric_summaries,
        })
    return evidence


def _decoded(value: Any, default: Any) -> Any:
    """Decode a JSON column, tolerating a backend that already decoded it."""
    if not isinstance(value, (str, bytes)):
        return value if value is not None else default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default
