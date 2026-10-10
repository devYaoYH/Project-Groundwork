"""Bounded candidate transport; allocation semantics belong to the backend."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from pydantic import Field, field_validator

from a2a_engine.remote.contract import CapabilityClaims, PROTOCOL_VERSION_V2, ToolOutcome, TurnInvocationV2, WireModel
from a2a_engine.remote.dispatch import DeliveryFailure, TurnDispatcherV2
from a2a_engine.remote.turns import ToolSpec, TurnRecorder

OPERATIONS = frozenset({'initialize_session', 'reset_context', 'project_update', 'error_feedback',
                        'cheap_talk', 'decide_allocation', 'notify_round_result', 'reflect'})
OPTIONAL_METHODS = frozenset({'reset_context', '_append_user', 'notify_round_result', 'reflect'})


class ApiMetadata(WireModel):
    model: str | None = Field(default=None, max_length=256)
    duration_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    reasoning: str | None = Field(default=None, max_length=32768)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    cached_prompt_tokens: int | None = Field(default=None, ge=0)
    retry_count: int | None = Field(default=None, ge=0, le=1000)
    error_type: str | None = Field(default=None, max_length=128)
    error_message: str | None = Field(default=None, max_length=256)
    used_fallback: bool | None = None


class CandidateOutput(WireModel):
    value: str | dict | None = None
    system_prompt: str | None = Field(default=None, max_length=32768)
    supported: list[str] = Field(default_factory=list, max_length=4)
    thinking: str = Field(default='', max_length=32768)
    speech: str = Field(default='', max_length=32768)
    reasoning: str = Field(default='', max_length=32768)
    api_meta: ApiMetadata = Field(default_factory=ApiMetadata)
    stream_started: bool = False

    @field_validator('supported')
    @classmethod
    def optional_methods(cls, values):
        if not set(values) <= OPTIONAL_METHODS:
            raise ValueError('unsupported optional behavior')
        return values

    @field_validator('value')
    @classmethod
    def bounded_candidate(cls, value):
        import json
        if len(json.dumps(value).encode()) > 32768:
            raise ValueError('candidate too large')
        if isinstance(value, dict):
            for key, quantity in value.items():
                if len(key) > 128:
                    raise ValueError('candidate key too long')
                entries = quantity if key == 'projects' and isinstance(quantity, dict) else {key: quantity}
                if len(entries) > 128 or any(len(k) > 128 or type(v) is not int or abs(v) > 10**12 for k, v in entries.items()):
                    raise ValueError('candidate must contain bounded integer quantities')
        return value


NEGOTIATION_TOOLS = (ToolSpec('submit_candidate', 'env', CandidateOutput,
                            frozenset(name.upper() for name in OPERATIONS), 'end_of_turn'),)


def stage_candidate(spec, parsed, recorder):
    operation = recorder.invocation.operation
    if operation in {'cheap_talk', 'reflect'} and not isinstance(parsed.value, str):
        return recorder.reject('invalid_candidate', 'text result required'), None
    if operation == 'decide_allocation' and not isinstance(parsed.value, dict):
        return recorder.reject('invalid_candidate', 'allocation candidate required'), None
    if any('action' in record for record in recorder.records):
        return recorder.reject('already_submitted', 'one result per invocation'), None
    return ToolOutcome(status='accepted', resolves='end_of_turn'), parsed.model_dump()


class RemoteNegotiator:
    def __init__(self, runtime, seat_id, *, timeout_s=120):
        self.runtime, self.seat_id, self.timeout_s = runtime, seat_id, timeout_s
        self.supported = set()
        self.last_thinking = self.last_speech = self.last_reasoning = ''
        self.last_api_meta = {}
        self._on_stream_start = None
        self.records = []
        self.dispatcher = TurnDispatcherV2(runtime.io, runtime.environment.registry.lifecycle,
                                           network=runtime.environment.network)

    def _invoke(self, operation, observation):
        episode = self.runtime.episode
        root = f'{self.runtime.io.base_url}/episodes/{episode.episode_id}'
        lifecycle = operation in {'register', 'episode_end'}
        if not lifecycle and operation not in OPERATIONS:
            raise ValueError('unsupported Negotiation operation')
        invocation = TurnInvocationV2(environment_id='negotiation', episode_id=episode.episode_id,
            attempt_id=episode.attempt_id, seat=self.seat_id, turn_id=uuid.uuid4().hex,
            kind=operation if lifecycle else 'turn', operation=operation,
            phase=None if lifecycle else operation.upper(), observation=observation,
            deadline=datetime.now(timezone.utc) + timedelta(seconds=self.timeout_s),
            mcp={name: f'{root}/{name}/mcp' for name in ('env', 'comm', 'lifecycle')})
        seat = episode.wait_ready(self.seat_id)
        if seat.protocol_version != PROTOCOL_VERSION_V2:
            raise RuntimeError('negotiation requires a2a-turns/2')
        recorder = None
        if not lifecycle:
            capabilities = self.runtime.environment.registry.capabilities
            claims = CapabilityClaims(episode_id=episode.episode_id, attempt_id=episode.attempt_id,
                seat=self.seat_id, turn_id=invocation.turn_id, phase=invocation.phase,
                allowed_tools=['env.submit_candidate'], exp=invocation.deadline.timestamp())
            invocation.capability = capabilities.mint(claims)
            recorder = TurnRecorder(invocation, claims, capabilities, stage_candidate)
            with episode.lock:
                seat.recorder = recorder
        try:
            completion = self.dispatcher.dispatch(seat, invocation, recorder)
        except DeliveryFailure:
            raise RuntimeError('negotiation runtime operation failed') from None
        finally:
            records = recorder.close() if recorder else []
            self.records.append(records)
        if completion is None:
            raise RuntimeError('negotiation runtime operation failed') from None
        if operation == 'register':
            seat.registered = True
        if lifecycle:
            return {}
        outputs = [record['action'] for record in records if 'action' in record]
        if len(outputs) != 1:
            raise RuntimeError('negotiation runtime candidate missing')
        return outputs[0]

    async def invoke(self, operation, observation):
        response = await asyncio.to_thread(self._invoke, operation, observation)
        if operation == 'initialize_session':
            self.supported = set(response['supported'])
        if operation in {'cheap_talk', 'decide_allocation'}:
            self.last_thinking, self.last_speech, self.last_reasoning = (response[name] for name in ('thinking', 'speech', 'reasoning'))
            self.last_api_meta = {k: v for k, v in response['api_meta'].items() if v is not None}
            if response['stream_started'] and self._on_stream_start:
                await self._on_stream_start()
        return response
