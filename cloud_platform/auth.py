"""Google identity verification and application sessions for the control pane.

Cookies carry signed, expiring claims only. Provider credentials never enter a
browser session. Email and display name are labels; Google subject is ownership.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from http.cookies import SimpleCookie
from typing import Any, Callable

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request

SESSION_COOKIE = "platform_session"
CSRF_COOKIE = "platform_csrf"


class Auth:
    def __init__(self, *, secret: str, client_id: str = "", backend: str = "local",
                 dev_auth: bool = False, session_ttl: int = 8 * 60 * 60,
                 verifier: Callable[[str, str], dict[str, Any]] | None = None):
        if backend != "local" and dev_auth:
            raise ValueError("Development login is only available with the local backend")
        if len(secret.encode()) < 32:
            raise ValueError("SESSION_SECRET must contain at least 32 bytes")
        if backend != "local" and not client_id:
            raise ValueError("GOOGLE_OAUTH_CLIENT_ID is required for hosted login")
        self.secret = secret.encode()
        self.client_id = client_id
        self.backend = backend
        self.dev_auth = dev_auth
        self.secure = backend != "local"
        self.session_ttl = session_ttl
        self.verifier = verifier or self._verify_google

    @classmethod
    def from_env(cls) -> "Auth":
        backend = os.getenv("PLATFORM_BACKEND", "local")
        dev = os.getenv("PLATFORM_DEV_AUTH", "false").lower() == "true"
        secret = os.getenv("SESSION_SECRET", "")
        if not secret and backend == "local" and dev:
            secret = "local-development-session-secret-change-for-google-login"
        return cls(secret=secret, backend=backend, dev_auth=dev,
                   client_id=os.getenv("GOOGLE_OAUTH_CLIENT_ID", ""))

    @staticmethod
    def _encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    def _sign(self, purpose: str, claims: dict[str, Any]) -> str:
        payload = self._encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
        signature = hmac.new(self.secret, f"{purpose}.{payload}".encode(), hashlib.sha256).digest()
        return f"{payload}.{self._encode(signature)}"

    def _read(self, purpose: str, value: str | None) -> dict[str, Any] | None:
        if not value or len(value) > 8192:
            return None
        try:
            payload, signature = value.split(".")
            expected = self._encode(hmac.new(self.secret, f"{purpose}.{payload}".encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(signature, expected):
                return None
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            if not isinstance(claims, dict) or not isinstance(claims.get("exp"), (int, float)):
                return None
            if claims["exp"] <= time.time():
                return None
            return claims
        except (ValueError, TypeError, UnicodeDecodeError):
            return None

    def _cookie(self, name: str, value: str, max_age: int) -> str:
        cookie = SimpleCookie()
        cookie[name] = value
        cookie[name]["path"] = "/"
        cookie[name]["max-age"] = max_age
        cookie[name]["httponly"] = True
        cookie[name]["samesite"] = "Lax"
        if self.secure:
            cookie[name]["secure"] = True
        return cookie[name].OutputString()

    def config(self, request: Request | None = None) -> tuple[dict[str, Any], str]:
        session = self._read("session", request.cookies.get(SESSION_COOKIE)) if request else None
        nonce = session.get("csrf") if session else secrets.token_urlsafe(32)
        claims = {"nonce": nonce, "exp": int(time.time()) + self.session_ttl}
        return ({"google_client_id": self.client_id, "dev_auth": self.dev_auth, "csrf_token": nonce},
                self._cookie(CSRF_COOKIE, self._sign("csrf", claims), self.session_ttl))

    def authenticate(self, request: Request) -> dict[str, str]:
        claims = self._read("session", request.cookies.get(SESSION_COOKIE))
        if not claims or not isinstance(claims.get("owner_id"), str) or not claims["owner_id"]:
            raise HTTPException(401, "Sign in to continue")
        return {key: claims.get(key, "") for key in ("owner_id", "email", "display_name")}

    def require_csrf(self, request: Request) -> str:
        challenge = self._read("csrf", request.cookies.get(CSRF_COOKIE))
        supplied = request.headers.get("x-csrf-token", "")
        if not challenge or not isinstance(challenge.get("nonce"), str) or not supplied:
            raise HTTPException(403, "Missing CSRF token")
        if not hmac.compare_digest(supplied, challenge["nonce"]):
            raise HTTPException(403, "Invalid CSRF token")
        session = self._read("session", request.cookies.get(SESSION_COOKIE))
        if session and not hmac.compare_digest(supplied, session.get("csrf", "")):
            raise HTTPException(403, "CSRF token does not match session")
        return supplied

    def _session(self, principal: dict[str, str], csrf: str) -> tuple[dict[str, str], str]:
        claims = {**principal, "csrf": csrf, "exp": int(time.time()) + self.session_ttl}
        return principal, self._cookie(SESSION_COOKIE, self._sign("session", claims), self.session_ttl)

    @staticmethod
    def _verify_google(credential: str, client_id: str) -> dict[str, Any]:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import id_token

        return id_token.verify_oauth2_token(credential, GoogleRequest(), client_id)

    async def login_google(self, request: Request) -> tuple[dict[str, str], str]:
        csrf = self.require_csrf(request)
        if not self.client_id:
            raise HTTPException(503, "Google login is not configured")
        body = await request.json()
        credential = body.get("credential") if isinstance(body, dict) else None
        if not isinstance(credential, str) or not credential or len(credential) > 16384:
            raise HTTPException(400, "Google credential is required")
        try:
            identity = await run_in_threadpool(self.verifier, credential, self.client_id)
        except Exception as exc:
            raise HTTPException(401, "Google credential could not be verified") from exc
        if (identity.get("iss") not in ("accounts.google.com", "https://accounts.google.com")
                or identity.get("aud") != self.client_id
                or not isinstance(identity.get("sub"), str) or not identity["sub"]
                or identity.get("email_verified") is not True):
            raise HTTPException(401, "Google identity is invalid")
        principal = {"owner_id": identity["sub"], "email": str(identity.get("email", "")),
                     "display_name": str(identity.get("name", identity.get("email", "Experimenter")))}
        return self._session(principal, csrf)

    async def login_dev(self, request: Request) -> tuple[dict[str, str], str]:
        if not self.dev_auth or self.backend != "local":
            raise HTTPException(404, "Not found")
        csrf = self.require_csrf(request)
        body = await request.json()
        user = body.get("user", "") if isinstance(body, dict) else ""
        if user not in ("Alice", "Bob"):
            raise HTTPException(400, "Choose Alice or Bob")
        return self._session({"owner_id": f"local:{user.lower()}", "email": f"{user.lower()}@localhost",
                              "display_name": user}, csrf)

    def logout_cookie(self) -> str:
        return self._cookie(SESSION_COOKIE, "", 0)
