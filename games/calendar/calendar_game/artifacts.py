"""Calendar-specific ingestion helpers for engine-owned derived artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from a2a_engine.schemas import GameTraceBase

from calendar_game.ratings import make_calendar_vps_artifact


class CalendarArtifactStore(Protocol):
    def get_trace(self, game_id: str) -> GameTraceBase | None:
        ...

    def put_derived_artifact(self, artifact: object) -> bool:
        ...


@dataclass(frozen=True)
class VPSIngestionResult:
    """Accounting for a replay-safe Calendar VPS ingestion pass."""

    written_game_ids: tuple[str, ...]
    unchanged_game_ids: tuple[str, ...]
    missing_trace_game_ids: tuple[str, ...]
    non_calendar_game_ids: tuple[str, ...]


def ingest_calendar_vps_by_game(
    store: CalendarArtifactStore,
    excess_vps_by_game: Mapping[str, Mapping[int | str, float]],
    *,
    metadata: Mapping[str, object] | None = None,
) -> VPSIngestionResult:
    """Attach post-hoc VPS results to their exact completed Calendar traces.

    The source trace is re-read from the store immediately before hashing. That
    is what prevents an analysis CSV produced for an old run revision from
    silently being joined to a replacement trace with the same ``game_id``.
    """
    written: list[str] = []
    unchanged: list[str] = []
    missing: list[str] = []
    non_calendar: list[str] = []
    for game_id in sorted(excess_vps_by_game):
        trace = store.get_trace(game_id)
        if trace is None:
            missing.append(game_id)
            continue
        if trace.config.game_name != "calendar":
            non_calendar.append(game_id)
            continue
        artifact = make_calendar_vps_artifact(
            trace.model_dump(mode="json"),
            excess_vps_by_game[game_id],
            metadata=metadata,
        )
        if store.put_derived_artifact(artifact):
            written.append(game_id)
        else:
            unchanged.append(game_id)
    return VPSIngestionResult(
        written_game_ids=tuple(written),
        unchanged_game_ids=tuple(unchanged),
        missing_trace_game_ids=tuple(missing),
        non_calendar_game_ids=tuple(non_calendar),
    )
