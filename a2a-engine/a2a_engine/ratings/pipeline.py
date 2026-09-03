"""Replayable rating materialization from completed episodes.

The engine deliberately does not rate a environment while it is running. A environment owns
the domain-specific trace-to-``RatingEvent`` mapping; this module owns replay
ordering, coverage checks, optional persistence, and snapshot construction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from a2a_engine.derived import DerivedArtifact, trace_digest
from a2a_engine.ratings.openskill import OpenSkillRater
from a2a_engine.ratings.schemas import MetricSpec, RatingEvent, RatingSnapshot
from a2a_engine.storage.base import iter_episodes


@runtime_checkable
class RatingAdapter(Protocol):
    """Environment-owned mapping from a durable trace into a generic rating event."""

    environment_id: str
    version: str
    metrics: Sequence[MetricSpec]

    def extract(
        self,
        trace: dict[str, Any],
        artifacts: Mapping[str, DerivedArtifact],
    ) -> RatingEvent | None:
        """Return a rating event, or ``None`` when a trace is not eligible."""
        ...


@runtime_checkable
class RatingMaterializationStore(Protocol):
    """Optional persistence operations used by the replay pipeline."""

    def get_derived_artifacts(self, episode_uid: str) -> list[DerivedArtifact]:
        ...

    def put_rating_event(
        self, event: RatingEvent, *, adapter_version: str, trace_digest: str
    ) -> bool:
        ...

    def put_rating_snapshot(
        self, snapshot: RatingSnapshot, *, environment_id: str, adapter_version: str
    ) -> None:
        ...


@dataclass(frozen=True)
class RatingMaterialization:
    """Result of replaying an adapter over the completed trace corpus."""

    snapshot: RatingSnapshot
    events: tuple[RatingEvent, ...]
    skipped_episode_uids: tuple[str, ...]
    suppressed_metric_names: tuple[str, ...]


def _artifact_map(
    store: object, episode_uid: str, digest: str
) -> dict[str, DerivedArtifact]:
    getter = getattr(store, "get_derived_artifacts", None)
    if getter is None:
        return {}
    return {
        artifact.key: artifact
        for artifact in getter(episode_uid)
        if artifact.trace_digest == digest
    }


def rebuild_rating_snapshot(store: object, adapter: RatingAdapter) -> RatingMaterialization:
    """Rebuild and persist a leaderboard snapshot from completed episodes only.

    A metric participates only when every eligible event contains scores for
    it. This prevents a metric such as privacy loss from appearing as a default
    OpenSkill value when its post-hoc artifact has not yet been produced.
    """
    events: list[RatingEvent] = []
    skipped: list[str] = []
    writer = store if isinstance(store, RatingMaterializationStore) else None
    for trace in iter_episodes(store, filters={"environment_id": adapter.environment_id}):
        payload = trace.model_dump(mode="json")
        digest = trace_digest(payload)
        event = adapter.extract(payload, _artifact_map(store, trace.episode_uid, digest))
        if event is None:
            skipped.append(trace.episode_uid)
            continue
        if event.episode_uid != trace.episode_uid:
            raise ValueError(
                f"{adapter.environment_id} rating adapter returned episode_uid {event.episode_uid!r} "
                f"for trace {trace.episode_uid!r}"
            )
        # Ordering and idempotency must come from the immutable source trace,
        # not from the wall clock at which a backfill happened.
        event.timestamp = trace.started_at
        if writer is not None:
            writer.put_rating_event(event, adapter_version=adapter.version, trace_digest=digest)
        events.append(event)

    available = set.intersection(
        *(set(event.metric_scores) for event in events)
    ) if events else set()
    active_metrics = [metric for metric in adapter.metrics if metric.name in available]
    suppressed = [metric.name for metric in adapter.metrics if metric.name not in available]
    snapshot = OpenSkillRater(active_metrics).rate_events(events)
    snapshot.metadata.update({
        "environment_id": adapter.environment_id,
        "adapter_version": adapter.version,
        "episode_count": len(events) + len(skipped),
        "rating_event_count": len(events),
        "skipped_episode_count": len(skipped),
        "suppressed_metric_names": suppressed,
        "source": "completed_trace_replay",
    })
    if writer is not None:
        writer.put_rating_snapshot(
            snapshot, environment_id=adapter.environment_id, adapter_version=adapter.version
        )
    return RatingMaterialization(
        snapshot=snapshot,
        events=tuple(events),
        skipped_episode_uids=tuple(skipped),
        suppressed_metric_names=tuple(suppressed),
    )
