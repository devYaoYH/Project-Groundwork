"""Typed, replay-only materialization of metrics declared by an release.

Live games write their native measurements to ``trace.metrics``.  Expensive or
evolving measurements run afterwards against the completed immutable trace and
are stored as digest-bound ``DerivedArtifact`` records.  This keeps a model
session independent of reporting, makes backfills idempotent, and gives every
new environment the same extension point.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from a2a_engine.derived import DerivedArtifact, trace_digest
from a2a_engine.environment import MeasureConfig
from a2a_engine.schemas import EpisodeTrace
from a2a_engine.storage.base import iter_episodes


MetricValue = float | dict[str, float]


class DerivedMetricResult(BaseModel):
    """Values emitted by one extractor for one completed episode."""

    model_config = ConfigDict(extra="forbid")

    values: dict[str, MetricValue] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class DerivedMetricExtractor(Protocol):
    """A deterministic, trace-only post-episode analysis implementation."""

    identifier: str
    version: str

    def extract(
        self, trace: dict[str, Any], artifacts: Mapping[str, DerivedArtifact]
    ) -> DerivedMetricResult | None:
        ...


class DerivedMetricRegistry:
    def __init__(self) -> None:
        self._extractors: dict[str, DerivedMetricExtractor] = {}

    def register(self, extractor: DerivedMetricExtractor) -> None:
        self._extractors[extractor.identifier] = extractor

    def get(self, identifier: str) -> DerivedMetricExtractor:
        try:
            return self._extractors[identifier]
        except KeyError as exc:
            raise KeyError(f"No derived metric extractor named {identifier!r}") from exc

    def identifiers(self) -> list[str]:
        return sorted(self._extractors)


derived_metrics = DerivedMetricRegistry()


def register_derived_metric_extractor(extractor: DerivedMetricExtractor) -> None:
    derived_metrics.register(extractor)


@dataclass(frozen=True)
class DerivedMetricMaterialization:
    episode_uid: str
    artifact: DerivedArtifact | None
    changed: bool
    skipped_reason: str | None = None


def _environment_metrics(trace: EpisodeTrace) -> list[MeasureConfig]:
    release = trace.release
    if release is None:
        return []
    raw = release.model_dump(mode="json").get("metrics", [])
    return [MeasureConfig.model_validate(metric) for metric in raw]


def _artifact_map(store: object, episode_uid: str, digest: str) -> dict[str, DerivedArtifact]:
    getter = getattr(store, "get_derived_artifacts", None)
    if getter is None:
        return {}
    return {
        artifact.key: artifact
        for artifact in getter(episode_uid)
        if artifact.trace_digest == digest
    }


def materialize_derived_metrics(
    store: object, *, environment_id: str | None = None
) -> list[DerivedMetricMaterialization]:
    """Materialize every declared derived metric from completed stored episodes.

    Multiple declared metrics can share an extractor; it runs once per trace.
    Results are persisted as ``derived_metrics.<identifier>@<version>`` when
    the store supports artifacts. Missing extractors are reported rather than
    silently treated as zero-valued measurements.
    """
    results: list[DerivedMetricMaterialization] = []
    filters = {"environment_id": environment_id} if environment_id else None
    for trace in iter_episodes(store, filters=filters):
        declared = [metric for metric in _environment_metrics(trace) if metric.producer == "derived"]
        if not declared:
            continue
        digest = trace_digest(trace)
        by_extractor: dict[str, list[MeasureConfig]] = {}
        for metric in declared:
            assert metric.extractor is not None  # enforced by MeasureConfig
            by_extractor.setdefault(metric.extractor, []).append(metric)
        artifact_inputs = _artifact_map(store, trace.episode_uid, digest)
        payload = trace.model_dump(mode="json")
        for identifier, metrics in by_extractor.items():
            try:
                extractor = derived_metrics.get(identifier)
            except KeyError:
                results.append(DerivedMetricMaterialization(
                    episode_uid=trace.episode_uid, artifact=None, changed=False,
                    skipped_reason=f"unregistered extractor: {identifier}",
                ))
                continue
            extracted = extractor.extract(payload, artifact_inputs)
            if extracted is None:
                results.append(DerivedMetricMaterialization(
                    episode_uid=trace.episode_uid, artifact=None, changed=False,
                    skipped_reason=f"extractor returned no result: {identifier}",
                ))
                continue
            required = {metric.name for metric in metrics}
            missing = sorted(required - set(extracted.values))
            if missing:
                raise ValueError(
                    f"{identifier} did not produce declared metrics {missing} for {trace.episode_uid}"
                )
            artifact = DerivedArtifact(
                episode_uid=trace.episode_uid,
                kind=f"derived_metrics.{identifier}",
                version=extractor.version,
                trace_digest=digest,
                payload={"values": extracted.values},
                metadata={"extractor": identifier, **extracted.metadata},
            )
            writer = getattr(store, "put_derived_artifact", None)
            changed = bool(writer(artifact)) if writer is not None else False
            results.append(DerivedMetricMaterialization(trace.episode_uid, artifact, changed))
    return results
