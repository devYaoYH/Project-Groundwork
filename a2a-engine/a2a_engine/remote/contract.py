"""Public a2a-turns/1 wire models, independent of any game."""

from datetime import datetime, timezone
from typing import Annotated, Literal
from a2a_engine.content import contains_image, validate_observation, SeatObservation, ChatMessage, TextPart, ImagePart

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema, field_serializer, field_validator

PROTOCOL_VERSION = "a2a-turns/1"
PROTOCOL_VERSION_V2 = "a2a-turns/2"
MAX_BODY_BYTES = 262144
MAX_MEDIA_BODY_BYTES = 8 * 1024 * 1024


def valid_body_size(body: bytes):
    if len(body) <= MAX_BODY_BYTES:
        return True
    if len(body) > MAX_MEDIA_BODY_BYTES:
        return False
    import json
    from a2a_engine.content import ImagePart
    try:
        document = json.loads(body)
        # Only an invocation observation can enlarge the request budget.
        if document.get("method") == "tools/call" and document.get("params", {}).get("name") == "start_turn":
            document = document["params"]["arguments"]
        if document.get("kind") == "register" or document.get("kind") not in {"round_start", "turn", "reflect", "episode_end"}:
            return False
        images = []
        def visit(node):
            if isinstance(node, dict):
                if node.get("type") == "image":
                    image = ImagePart.model_validate(node)
                    if image.data is None:
                        raise ValueError("wire image requires bytes")
                    images.append(image)
                else:
                    for child in node.values(): visit(child)
            elif isinstance(node, list):
                for child in node: visit(child)
        visit(document.get("observation", {}))
        # Retain the old text/metadata limit even in an image-bearing request.
        from a2a_engine.content import media_metadata
        return bool(images) and len(json.dumps(media_metadata(document)).encode()) <= MAX_BODY_BYTES
    except (ValueError, TypeError, AttributeError, RecursionError):
        return False


def observation_schema():
    schema = SeatObservation.model_json_schema()
    definitions = schema.pop("$defs", {})
    def inline(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return inline(definitions[node["$ref"].rsplit("/", 1)[-1]])
            return {key: inline(value) for key, value in node.items()}
        if isinstance(node, list):
            return [inline(value) for value in node]
        return node
    return inline(schema)


Observation = Annotated[dict, WithJsonSchema(observation_schema())]


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class JoinRequest(WireModel):
    join_ticket: str = Field(min_length=32, max_length=256)
    callback_url: str = Field(max_length=2048)
    protocol_versions: list[str] = Field(min_length=1, max_length=16)
    agent_info: dict[str, str] = Field(default_factory=dict, max_length=16)
    input_modalities: set[str] = Field(default_factory=lambda: {"text"}, max_length=16)

    @field_serializer("input_modalities")
    def stable_inputs(self, value):
        return sorted(value)

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
    observation: Observation = Field(default_factory=dict)
    prompt: str | None = None
    inbox: list[dict] = Field(default_factory=list)
    mcp: dict[str, str] = Field(default_factory=dict)
    capability: str | None = None

    @field_validator("inbox")
    @classmethod
    def text_only_inbox(cls, value):
        if contains_image(value):
            raise ValueError("image inboxes are unsupported; use private observation.content")
        return value

    @field_validator("observation")
    @classmethod
    def typed_content(cls, value):
        return validate_observation(value)

    @field_validator("deadline")
    @classmethod
    def aware_deadline(cls, value):
        if value.tzinfo is None:
            raise ValueError("deadline must include timezone")
        return value.astimezone(timezone.utc)


class TokenUsage(WireModel):
    prompt_tokens: int = Field(default=0, strict=True, ge=0, le=1000000000)
    completion_tokens: int = Field(default=0, strict=True, ge=0, le=1000000000)
    total_tokens: int = Field(default=0, strict=True, ge=0, le=1000000000)
    reasoning_tokens: int | None = Field(default=None, strict=True, ge=0, le=1000000000)
    cached_prompt_tokens: int | None = Field(default=None, strict=True, ge=0, le=1000000000)


class Telemetry(WireModel):
    latency_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    usage: TokenUsage | None = None


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


class TurnIdentity(WireModel):
    protocol_version: Literal["a2a-turns/2"] = PROTOCOL_VERSION_V2
    environment_id: str = Field(min_length=1, max_length=128)
    episode_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    seat: int = Field(strict=True, ge=0)
    turn_id: str = Field(min_length=1, max_length=128)


class HelloV2(Hello):
    protocol_version: Literal["a2a-turns/2"] = PROTOCOL_VERSION_V2
    environment_id: str
    attempt_id: str


class JoinResponseV2(JoinResponse):
    protocol_version: Literal["a2a-turns/2"] = PROTOCOL_VERSION_V2
    environment_id: str
    attempt_id: str
    seat_secret: str = Field(repr=False)


class TurnInvocationV2(TurnIdentity):
    kind: Literal["register", "round_start", "turn", "reflect", "episode_end"]
    operation: Literal["register", "observe", "act", "reflect", "episode_end", "probe",
                       "initialize_session", "reset_context", "project_update", "error_feedback",
                       "cheap_talk", "decide_allocation", "notify_round_result"]
    phase: str | None = Field(default=None, max_length=128)
    parent_phase: str | None = Field(default=None, max_length=128)
    deadline: datetime
    observation: Observation = Field(default_factory=dict)
    prompt: str | None = None
    inbox: list[dict] = Field(default_factory=list)
    mcp: dict[str, str]
    capability: str | None = Field(default=None, repr=False)
    lifecycle_credential: str | None = Field(default=None, repr=False)

    @field_validator("inbox")
    @classmethod
    def text_only_inbox(cls, value):
        if contains_image(value):
            raise ValueError("image inboxes are unsupported; use private observation.content")
        return value

    @field_validator("observation")
    @classmethod
    def typed_content(cls, value):
        return validate_observation(value)

    @field_validator("deadline")
    @classmethod
    def aware_deadline(cls, value):
        return TurnInvocation.aware_deadline(value)

    def identity(self):
        return TurnIdentity.model_validate({key: getattr(self, key) for key in TurnIdentity.model_fields})


class TurnAcceptance(TurnIdentity):
    status: Literal["accepted", "rejected"]
    reason_code: Literal["busy", "capacity", "conflict", "expired", "invalid", "registration_required", "ended"] | None = None


class TurnFinished(TurnIdentity):
    status: Literal["succeeded", "failed"]
    failure_code: Literal["execution_failed", "deadline", "cancelled"] | None = None
    telemetry: Telemetry = Field(default_factory=Telemetry)
    reflection: dict | None = None


class CompletionReceipt(TurnIdentity):
    disposition: Literal["recorded", "duplicate", "closed"]


def contract_schemas(protocol_version=PROTOCOL_VERSION) -> dict:
    if protocol_version == PROTOCOL_VERSION_V2:
        return {model.__name__: model.model_json_schema() for model in (
            SeatObservation, ChatMessage, TextPart, ImagePart, JoinRequest, HelloV2, HelloAcceptance, JoinResponseV2, TurnInvocationV2,
            TurnAcceptance, TurnFinished, CompletionReceipt, ToolOutcome, ProtocolError, CapabilityClaims,
        )}
    if protocol_version != PROTOCOL_VERSION:
        raise ValueError("unsupported protocol version")
    return {model.__name__: model.model_json_schema() for model in (
        SeatObservation, ChatMessage, TextPart, ImagePart, JoinRequest, Hello, HelloAcceptance, JoinResponse, TurnInvocation,
        TurnCompletion, ToolOutcome, ProtocolError, CapabilityClaims,
    )}
