"""Calendar-specific ingestion helpers for engine-owned derived artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from a2a_engine.schemas import EpisodeTrace

from calendar_game.ratings import make_calendar_vps_artifact


class CalendarArtifactStore(Protocol):
    def get_episode(self, episode_uid: str) -> EpisodeTrace | None:
        ...

    def put_derived_artifact(self, artifact: object) -> bool:
        ...


@dataclass(frozen=True)
class VPSIngestionResult:
    """Accounting for a replay-safe Calendar VPS ingestion pass."""

    written_episode_uids: tuple[str, ...]
    unchanged_episode_uids: tuple[str, ...]
    missing_trace_episode_uids: tuple[str, ...]
    non_calendar_episode_uids: tuple[str, ...]


def ingest_calendar_vps_by_game(
    store: CalendarArtifactStore,
    excess_vps_by_game: Mapping[str, Mapping[int | str, float]],
    *,
    metadata: Mapping[str, object] | None = None,
) -> VPSIngestionResult:
    """Attach post-hoc VPS results to their exact completed Calendar episodes.

    The source trace is re-read from the store immediately before hashing. That
    is what prevents an analysis CSV produced for an old run revision from
    silently being joined to a replacement trace with the same ``episode_uid``.
    """
    written: list[str] = []
    unchanged: list[str] = []
    missing: list[str] = []
    non_calendar: list[str] = []
    for episode_uid in sorted(excess_vps_by_game):
        trace = store.get_episode(episode_uid)
        if trace is None:
            missing.append(episode_uid)
            continue
        if trace.config.environment_id != "calendar":
            non_calendar.append(episode_uid)
            continue
        artifact = make_calendar_vps_artifact(
            trace.model_dump(mode="json"),
            excess_vps_by_game[episode_uid],
            metadata=metadata,
        )
        if store.put_derived_artifact(artifact):
            written.append(episode_uid)
        else:
            unchanged.append(episode_uid)
    return VPSIngestionResult(
        written_episode_uids=tuple(written),
        unchanged_episode_uids=tuple(unchanged),
        missing_trace_episode_uids=tuple(missing),
        non_calendar_episode_uids=tuple(non_calendar),
    )
