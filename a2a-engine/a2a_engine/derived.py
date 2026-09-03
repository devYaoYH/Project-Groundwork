"""Versioned, trace-derived artifacts.

Experiment episodes are immutable source records. Expensive or evolving analyses
(privacy scores, judge results, embeddings) belong in a separate artifact
record keyed to both the trace and the extractor version. This makes replay and
backfill explicit instead of letting a live environment session mutate reporting state.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from a2a_engine.schemas import EpisodeTrace


def trace_digest(trace: EpisodeTrace | dict[str, Any]) -> str:
    """Return a stable digest for the complete persisted trace payload."""
    if isinstance(trace, EpisodeTrace):
        payload = trace.model_dump(mode="json")
    else:
        payload = trace
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class DerivedArtifact(BaseModel):
    """A reproducible analysis result derived from one completed environment trace."""

    episode_uid: str
    kind: str
    version: str
    trace_digest: str
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def key(self) -> str:
        return f"{self.kind}@{self.version}"
