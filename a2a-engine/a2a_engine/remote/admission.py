"""One-time ticket challenge, followed by an explicit credential-ready barrier."""

import secrets
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .contract import Hello, HelloV2, HelloAcceptance, JoinResponse, JoinResponseV2, PROTOCOL_VERSION, PROTOCOL_VERSION_V2
from .dispatch import loopback_url, post_signed


async def join(context, request, base_url, *, network=None):
    callback = network.validate(request.callback_url) if network else loopback_url(request.callback_url)
    version = PROTOCOL_VERSION_V2 if PROTOCOL_VERSION_V2 in request.protocol_versions else PROTOCOL_VERSION
    if version not in request.protocol_versions or (network and network.advertised_origin and version != PROTOCOL_VERSION_V2):
        raise ValueError("unsupported protocol version")
    if version == PROTOCOL_VERSION_V2 and urlsplit(callback).path:
        raise ValueError("callback must be a provisioned service origin")
    with context.lock:
        seat = next((s for s in context.seats.values() if secrets.compare_digest(s.ticket, request.join_ticket)), None)
        if not context.active or seat is None or seat.consumed or seat.join_deadline <= time.time():
            raise ValueError("invalid, consumed, or expired join ticket")
        if not seat.required_modalities <= request.input_modalities:
            raise ValueError("remote seat does not support required input modalities")
        seat.input_modalities = set(request.input_modalities)
        seat.consumed = True
    extra = {"environment_id": context.environment_id, "attempt_id": context.attempt_id} if version == PROTOCOL_VERSION_V2 else {}
    hello_cls = HelloV2 if extra else Hello
    hello = hello_cls(episode_id=context.episode_id, seat=seat.seat, turn_id=uuid.uuid4().hex,
                  **extra,
                  deadline=datetime.fromtimestamp(seat.join_deadline, timezone.utc), nonce=secrets.token_urlsafe(32))
    acceptance = HelloAcceptance.model_validate_json(await post_signed(callback + "/hello", hello, seat.ticket, network=network))
    if not acceptance.accept or not secrets.compare_digest(acceptance.nonce, hello.nonce):
        raise ValueError("callback did not accept challenge")
    with context.lock:
        if not context.active or time.time() >= seat.join_deadline:
            raise ValueError("join deadline passed")
        seat.secret = secrets.token_urlsafe(32)
        seat.callback_url = callback
        seat.protocol_version = version
        seat.agent_info = {key: value.replace(request.join_ticket, "[redacted]").replace(callback, "[redacted]")
                           for key, value in request.agent_info.items()}
        root = f"{base_url}/episodes/{context.episode_id}"
        response_cls = JoinResponseV2 if extra else JoinResponse
        return response_cls(episode_id=context.episode_id, seat=seat.seat, seat_secret=seat.secret, **extra,
                            mcp={name: f"{root}/{name}/mcp" for name in (("env", "comm", "lifecycle") if extra else ("env", "comm"))},
                            ready_url=f"{root}/ready")
