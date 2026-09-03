"""Generic, environment-agnostic schemas for agent-to-agent experiments.

Concrete benchmarks subclass these — e.g. a calendar-scheduling benchmark
extends ``EpisodeConfigBase`` to add its own fields, and writes per-environment payloads
into ``Event.data`` and ``EpisodeTrace.final_state``/``metrics``.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny


class ParticipantBinding(BaseModel):
    """Per-agent configuration entry inside a GameConfig."""

    model_config = ConfigDict(extra="allow")

    type: str = "llm"  # "llm" | "human" | "heuristic" | "random" | environment-defined
    model: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class EpisodeConfigBase(BaseModel):
    """Base environment config. Subclass per benchmark to add environment-specific fields."""

    model_config = ConfigDict(extra="allow")

    environment_id: str
    num_agents: int
    agents: list[ParticipantBinding] = Field(default_factory=list)
    seed: int | None = None
    episode_id: str | None = None
    experiment_name: str | None = None
    git_hash: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class Event(BaseModel):
    """A single event in a environment trace.

    ``type`` is a environment-defined string, e.g. "message", "broadcast",
    "task_injected", "decision", "round_start". Environment-specific payload lives in
    ``data``.
    """

    model_config = ConfigDict(extra="allow")

    type: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    data: dict[str, Any] = Field(default_factory=dict)


class ReleaseInputReference(BaseModel):
    """The input artifact declaration copied from an ReleaseDeclaration."""

    model_config = ConfigDict(extra="forbid")

    id: str
    path: str
    sha256: str


class ReleaseReference(BaseModel):
    """Immutable release identity attached to every typed-config trace."""

    model_config = ConfigDict(extra="allow")

    schema_version: int = 1
    id: str
    release: str
    content_sha256: str
    inputs: list[ReleaseInputReference] = Field(default_factory=list)


class EpisodeReference(BaseModel):
    """The concrete episode within an experiment that produced this trace."""

    model_config = ConfigDict(extra="forbid")

    id: str
    experiment_name: str
    cell_id: str
    episode_idx: int


class EpisodeTrace(BaseModel):
    """Persisted record of a single environment run."""

    model_config = ConfigDict(extra="allow")

    episode_uid: str
    config: SerializeAsAny[EpisodeConfigBase]
    events: list[Event] = Field(default_factory=list)
    final_state: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    release: ReleaseReference | None = None
    episode: EpisodeReference | None = None
    # Operational correlation is kept separate from benchmark semantics. These
    # fields identify external telemetry without making an OTLP exporter the
    # source of truth for a environment result.
    observability: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: datetime | None = None
    stopped: bool = False
