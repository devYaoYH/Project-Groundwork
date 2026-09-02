"""Generic, game-agnostic schemas for agent-to-agent experiments.

Concrete benchmarks subclass these — e.g. a calendar-scheduling benchmark
extends ``GameConfigBase`` to add its own fields, and writes per-game payloads
into ``GameEvent.data`` and ``GameTraceBase.final_state``/``metrics``.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny


class AgentInfo(BaseModel):
    """Per-agent configuration entry inside a GameConfig."""

    model_config = ConfigDict(extra="allow")

    type: str = "llm"  # "llm" | "human" | "heuristic" | "random" | game-defined
    model: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class GameConfigBase(BaseModel):
    """Base game config. Subclass per benchmark to add game-specific fields."""

    model_config = ConfigDict(extra="allow")

    game_name: str
    num_agents: int
    agents: list[AgentInfo] = Field(default_factory=list)
    seed: int | None = None
    experiment_run_id: str | None = None
    experiment_name: str | None = None
    git_hash: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class GameEvent(BaseModel):
    """A single event in a game trace.

    ``type`` is a game-defined string, e.g. "message", "broadcast",
    "task_injected", "decision", "round_start". Game-specific payload lives in
    ``data``.
    """

    model_config = ConfigDict(extra="allow")

    type: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    data: dict[str, Any] = Field(default_factory=dict)


class EnvironmentInputReference(BaseModel):
    """The input artifact declaration copied from an EnvironmentConfig."""

    model_config = ConfigDict(extra="forbid")

    id: str
    path: str
    sha256: str


class EnvironmentReference(BaseModel):
    """Immutable environment identity attached to every typed-config trace."""

    model_config = ConfigDict(extra="allow")

    schema_version: int = 1
    id: str
    revision: str
    content_sha256: str
    inputs: list[EnvironmentInputReference] = Field(default_factory=list)


class EpisodeReference(BaseModel):
    """The concrete episode within an experiment that produced this trace."""

    model_config = ConfigDict(extra="forbid")

    id: str
    experiment_name: str
    batch_label: str
    run_idx: int


class GameTraceBase(BaseModel):
    """Persisted record of a single game run."""

    model_config = ConfigDict(extra="allow")

    game_id: str
    config: SerializeAsAny[GameConfigBase]
    events: list[GameEvent] = Field(default_factory=list)
    final_state: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    environment: EnvironmentReference | None = None
    episode: EpisodeReference | None = None
    # Operational correlation is kept separate from benchmark semantics. These
    # fields identify external telemetry without making an OTLP exporter the
    # source of truth for a game result.
    observability: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=datetime.utcnow)
    ended_at: datetime | None = None
    stopped: bool = False
