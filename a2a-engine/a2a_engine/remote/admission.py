"""One-time ticket challenge, followed by an explicit credential-ready barrier."""

import secrets
import time
import uuid
from datetime import datetime, timezone

from .contract import Hello, HelloAcceptance, JoinResponse, PROTOCOL_VERSION
from .dispatch import loopback_url, post_signed


async def join(context, request, base_url):
    callback = loopback_url(request.callback_url)
    if PROTOCOL_VERSION not in request.protocol_versions:
        raise ValueError("unsupported protocol version")
    with context.lock:
        seat = next((s for s in context.seats.values() if secrets.compare_digest(s.ticket, request.join_ticket)), None)
        if not context.active or seat is None or seat.consumed or seat.join_deadline <= time.time():
            raise ValueError("invalid, consumed, or expired join ticket")
        seat.consumed = True
    hello = Hello(episode_id=context.episode_id, seat=seat.seat, turn_id=uuid.uuid4().hex,
                  deadline=datetime.fromtimestamp(seat.join_deadline, timezone.utc), nonce=secrets.token_urlsafe(32))
    acceptance = HelloAcceptance.model_validate_json(await post_signed(callback + "/hello", hello, seat.ticket))
    if not acceptance.accept or not secrets.compare_digest(acceptance.nonce, hello.nonce):
        raise ValueError("callback did not accept challenge")
    with context.lock:
        if not context.active or time.time() >= seat.join_deadline:
            raise ValueError("join deadline passed")
        seat.secret = secrets.token_urlsafe(32)
        seat.callback_url = callback
        seat.agent_info = request.agent_info
        root = f"{base_url}/episodes/{context.episode_id}"
        return JoinResponse(episode_id=context.episode_id, seat=seat.seat, seat_secret=seat.secret,
                            mcp={name: f"{root}/{name}/mcp" for name in ("env", "comm")},
                            ready_url=f"{root}/ready")
