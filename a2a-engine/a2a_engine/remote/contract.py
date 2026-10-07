"""Public a2a-turns/1 wire models, independent of any game."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROTOCOL_VERSION = "a2a-turns/1"
MAX_BODY_BYTES = 262144


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JoinRequest(WireModel):
    join_ticket: str = Field(min_length=32, max_length=256)
    callback_url: str = Field(max_length=2048)
    protocol_versions: list[str] = Field(min_length=1, max_length=16)
    agent_info: dict[str, str] = Field(default_factory=dict, max_length=16)

    @field_validator("agent_info")
    @classmethod
    def bounded_info(cls, value):
        if any(len(k) > 64 or len(v) > 256 for k, v in value.items()):
            raise ValueError("agent_info exceeds bounds")
        return {k: v for k, v in value.items() if k in {"name", "version", "implementation"}}


class Hello(WireModel):
    protocol_version: Literal["a2a-turns/1"] = PROTOCOL_VERSION
    episode_id: str
    seat: int
    turn_id: str
    deadline: datetime
    nonce: str


class HelloAcceptance(WireModel):
    nonce: str
    accept: bool


class JoinResponse(WireModel):
    protocol_version: Literal["a2a-turns/1"] = PROTOCOL_VERSION
    episode_id: str
    seat: int
    seat_secret: str
    mcp: dict[str, str]
    ready_url: str


class TurnInvocation(WireModel):
    protocol_version: Literal["a2a-turns/1"] = PROTOCOL_VERSION
    episode_id: str
    seat: int
    turn_id: str
    kind: Literal["register", "round_start", "turn", "reflect", "episode_end"]
    phase: str | None = None
    parent_phase: str | None = None
    deadline: datetime
    observation: dict = Field(default_factory=dict)
    prompt: str | None = None
    inbox: list[dict] = Field(default_factory=list)
    mcp: dict[str, str] = Field(default_factory=dict)
    capability: str | None = None

    @field_validator("deadline")
    @classmethod
    def aware_deadline(cls, value):
        if value.tzinfo is None:
            raise ValueError("deadline must include timezone")
        return value.astimezone(timezone.utc)


class Telemetry(WireModel):
    latency_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class TurnCompletion(WireModel):
    turn_id: str
    telemetry: Telemetry = Field(default_factory=Telemetry)
    reflection: dict | None = None


class ToolOutcome(WireModel):
    status: Literal["accepted", "rejected", "ok"]
    resolves: Literal["read_only", "immediate", "end_of_turn", "end_of_phase"]
    code: str | None = None
    reason: str | None = None
    data: dict | list | None = None


class ProtocolError(WireModel):
    code: str
    message: str


class CapabilityClaims(WireModel):
    episode_id: str
    attempt_id: str
    seat: int
    turn_id: str
    phase: str
    allowed_tools: list[str]
    exp: float


def contract_schemas() -> dict:
    return {model.__name__: model.model_json_schema() for model in (
        JoinRequest, Hello, HelloAcceptance, JoinResponse, TurnInvocation,
        TurnCompletion, ToolOutcome, ProtocolError, CapabilityClaims,
    )}
