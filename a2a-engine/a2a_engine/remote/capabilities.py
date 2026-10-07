"""Signed claims with explicit open-turn registration and revocation."""

import base64
import hashlib
import hmac
import secrets
import threading
import time

from .contract import CapabilityClaims


class AuthorizationError(ValueError):
    pass


class Capabilities:
    def __init__(self, *, clock=time.time):
        self._key = secrets.token_bytes(32)
        self._clock = clock
        self._open = {}
        self._lock = threading.RLock()

    def mint(self, claims: CapabilityClaims) -> str:
        body = base64.urlsafe_b64encode(claims.model_dump_json().encode()).decode()
        signature = hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()
        with self._lock:
            key = (claims.episode_id, claims.attempt_id, claims.seat)
            if key in self._open:
                raise AuthorizationError("seat already has an open turn")
            self._open[key] = claims.turn_id
        return f"{body}.{signature}"

    def verify(self, token: str, *, episode_id: str, attempt_id: str) -> CapabilityClaims:
        try:
            if len(token) > 8192:
                raise ValueError()
            body, signature = token.split(".")
            expected = hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError()
            claims = CapabilityClaims.model_validate_json(base64.urlsafe_b64decode(body))
        except Exception:
            raise AuthorizationError("invalid capability") from None
        with self._lock:
            if (claims.episode_id != episode_id or claims.attempt_id != attempt_id
                    or claims.exp <= self._clock()
                    or self._open.get((episode_id, attempt_id, claims.seat)) != claims.turn_id):
                raise AuthorizationError("capability is expired, closed, or out of scope")
        return claims

    def revoke(self, claims: CapabilityClaims):
        with self._lock:
            key = (claims.episode_id, claims.attempt_id, claims.seat)
            if self._open.get(key) == claims.turn_id:
                del self._open[key]
