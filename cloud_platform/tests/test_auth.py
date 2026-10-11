from __future__ import annotations

import json
import time
from http.cookies import SimpleCookie

import pytest
from starlette.exceptions import HTTPException
from starlette.requests import Request

from cloud_platform.auth import Auth, CSRF_COOKIE, SESSION_COOKIE


def cookie_value(header: str) -> tuple[str, str]:
    cookie = SimpleCookie(header)
    key = next(iter(cookie))
    return key, cookie[key].value


def request(*, cookies=None, csrf="", body=None):
    headers = []
    if cookies:
        headers.append((b"cookie", "; ".join(f"{key}={value}" for key, value in cookies.items()).encode()))
    if csrf:
        headers.append((b"x-csrf-token", csrf.encode()))
    payload = json.dumps(body or {}).encode()

    async def receive():
        return {"type": "http.request", "body": payload}

    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers}, receive)


def challenge(auth):
    config, header = auth.config()
    key, value = cookie_value(header)
    return config["csrf_token"], {key: value}


def local_auth():
    return Auth(secret="x" * 32, dev_auth=True)


@pytest.mark.asyncio
async def test_config_reuses_prelogin_challenge_across_tabs():
    auth = local_auth()
    original_csrf, cookies = challenge(auth)
    config, header = auth.config(request(cookies=cookies))
    cookies.update([cookie_value(header)])
    assert config["csrf_token"] == original_csrf
    alice, _ = await auth.login_dev(request(cookies=cookies, csrf=original_csrf, body={"user": "Alice"}))
    assert alice["owner_id"] == "local:alice"


def test_config_replaces_expired_prelogin_challenge():
    auth = local_auth()
    expired = auth._sign("csrf", {"nonce": "expired-challenge", "exp": time.time() - 1})
    config, header = auth.config(request(cookies={CSRF_COOKIE: expired}))
    assert config["csrf_token"] != "expired-challenge"
    assert auth.require_csrf(request(cookies=dict([cookie_value(header)]), csrf=config["csrf_token"])) == config["csrf_token"]


@pytest.mark.asyncio
async def test_config_session_nonce_takes_precedence_over_another_valid_challenge():
    auth = local_auth()
    csrf, cookies = challenge(auth)
    _, session_header = await auth.login_dev(request(cookies=cookies, csrf=csrf, body={"user": "Alice"}))
    alternate_csrf, alternate_cookies = challenge(auth)
    assert alternate_csrf != csrf
    alternate_cookies.update([cookie_value(session_header)])
    config, csrf_header = auth.config(request(cookies=alternate_cookies))
    assert config["csrf_token"] == csrf
    alternate_cookies.update([cookie_value(csrf_header)])
    assert auth.require_csrf(request(cookies=alternate_cookies, csrf=csrf)) == csrf


@pytest.mark.asyncio
async def test_dev_login_and_session_expiry():
    auth = local_auth()
    csrf, cookies = challenge(auth)
    alice, header = await auth.login_dev(request(cookies=cookies, csrf=csrf, body={"user": "Alice"}))
    cookies.update([cookie_value(header)])
    assert alice["owner_id"] == "local:alice"
    assert auth.authenticate(request(cookies=cookies)) == alice
    assert "HttpOnly" in header and "SameSite=Lax" in header
    assert "Secure" not in header
    expired = auth._sign("session", {**alice, "exp": time.time() - 1})
    with pytest.raises(HTTPException) as error:
        auth.authenticate(request(cookies={SESSION_COOKIE: expired}))
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_session_cannot_be_forged_or_used_as_csrf():
    auth = local_auth()
    csrf, cookies = challenge(auth)
    principal, header = await auth.login_dev(request(cookies=cookies, csrf=csrf, body={"user": "Alice"}))
    _, session = cookie_value(header)
    for invalid in (session + "x", "malformed", auth._sign("csrf", {**principal, "exp": time.time() + 99})):
        with pytest.raises(HTTPException) as error:
            auth.authenticate(request(cookies={SESSION_COOKIE: invalid}))
        assert error.value.status_code == 401
    with pytest.raises(HTTPException):
        auth.require_csrf(request(cookies={CSRF_COOKIE: session}, csrf=csrf))


@pytest.mark.asyncio
async def test_csrf_challenge_required_for_login_and_bound_to_session():
    auth = local_auth()
    csrf, cookies = challenge(auth)
    for candidate in (request(body={"user": "Alice"}), request(cookies=cookies, csrf="wrong", body={"user": "Alice"})):
        with pytest.raises(HTTPException) as error:
            await auth.login_dev(candidate)
        assert error.value.status_code == 403
    _, header = await auth.login_dev(request(cookies=cookies, csrf=csrf, body={"user": "Alice"}))
    cookies.update([cookie_value(header)])
    assert auth.require_csrf(request(cookies=cookies, csrf=csrf)) == csrf
    next_config, next_header = auth.config(request(cookies=cookies))
    assert next_config["csrf_token"] == csrf
    alternate_csrf, alternate_cookies = challenge(auth)
    alternate_cookies[SESSION_COOKIE] = cookies[SESSION_COOKIE]
    with pytest.raises(HTTPException) as error:
        auth.require_csrf(request(cookies=alternate_cookies, csrf=alternate_csrf))
    assert error.value.status_code == 403


def test_hosted_configuration_fails_closed(monkeypatch):
    with pytest.raises(ValueError, match="Development login"):
        Auth(secret="x" * 32, client_id="client", backend="gcp", dev_auth=True)
    with pytest.raises(ValueError, match="GOOGLE_OAUTH_CLIENT_ID"):
        Auth(secret="x" * 32, backend="gcp")
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        Auth(secret="short", client_id="client", backend="gcp")
    monkeypatch.setenv("PLATFORM_BACKEND", "gcp")
    monkeypatch.setenv("PLATFORM_DEV_AUTH", "false")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        Auth.from_env()


@pytest.mark.asyncio
async def test_hosted_dev_login_unavailable():
    auth = Auth(secret="x" * 32, backend="gcp", client_id="client")
    with pytest.raises(HTTPException) as error:
        await auth.login_dev(request(body={"user": "Alice"}))
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_google_ownership_uses_verified_subject_and_secure_cookie():
    identity = {"sub": "12345", "email": "alice@example.com", "email_verified": True,
                "name": "Alice", "iss": "https://accounts.google.com", "aud": "client"}
    calls = []

    def verify(credential, audience):
        calls.append((credential, audience))
        return identity

    auth = Auth(secret="x" * 32, backend="gcp", client_id="client", verifier=verify)
    csrf, cookies = challenge(auth)
    principal, header = await auth.login_google(request(cookies=cookies, csrf=csrf, body={"credential": "google-token"}))
    assert calls == [("google-token", "client")]
    assert principal["owner_id"] == "12345"
    assert principal["email"] == "alice@example.com"
    assert "Secure" in header and "HttpOnly" in header
    # Email changes retain the same user-owned workspace.
    identity["email"] = "new-address@example.com"
    next_principal, _ = await auth.login_google(request(cookies=cookies, csrf=csrf, body={"credential": "google-token"}))
    assert next_principal["owner_id"] == principal["owner_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("iss", "untrusted"), ("aud", "other-client"), ("sub", ""), ("email_verified", False)])
async def test_google_rejects_invalid_verified_claims(field, value):
    identity = {"sub": "12345", "email_verified": True, "iss": "accounts.google.com", "aud": "client"}
    identity[field] = value
    auth = Auth(secret="x" * 32, client_id="client", verifier=lambda *_: identity)
    csrf, cookies = challenge(auth)
    with pytest.raises(HTTPException) as error:
        await auth.login_google(request(cookies=cookies, csrf=csrf, body={"credential": "google-token"}))
    assert error.value.status_code == 401


@pytest.mark.asyncio
async def test_google_verifier_failure_does_not_expose_credential():
    def verify(*_):
        raise ValueError("sensitive-google-token")

    auth = Auth(secret="x" * 32, client_id="client", verifier=verify)
    csrf, cookies = challenge(auth)
    with pytest.raises(HTTPException) as error:
        await auth.login_google(request(cookies=cookies, csrf=csrf, body={"credential": "sensitive-google-token"}))
    assert error.value.status_code == 401
    assert "sensitive" not in error.value.detail
