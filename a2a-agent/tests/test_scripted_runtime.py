import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from a2a_engine.remote.contract import TurnInvocation
from a2a_engine.remote.dispatch import signed_headers


def invocation(harness, **overrides):
    admission = harness.runtimes[0].admission
    return TurnInvocation(episode_id=admission.episode_id, seat=0, turn_id="test-push", kind="register",
                          deadline=datetime.now(timezone.utc) + timedelta(seconds=30),
                          mcp=admission.mcp, observation={"game_config": {"communication_protocol": "dm"}},
                          **overrides)


def push(harness, value, *, key=None, body=None, headers=None):
    body = body or value.model_dump_json().encode()
    headers = headers or signed_headers(body, value.turn_id, value.deadline.isoformat(),
                                       key or harness.runtimes[0].admission.seat_secret)
    return httpx.post(harness.callbacks[0].base_url + "/turns", content=body, headers=headers, trust_env=False)


def test_registration_and_payload_sensitive_dedup(remote_harness):
    harness = remote_harness()
    value = invocation(harness)
    response = push(harness, value)
    assert response.status_code == 200
    assert push(harness, value).json() == response.json()
    assert len(harness.invocations) == 1
    changed = value.model_copy(update={"prompt": "different"})
    assert push(harness, changed).status_code == 409
    assert len(harness.invocations) == 1


@pytest.mark.parametrize("mutation", ["signature", "body", "id", "deadline", "expired", "seat", "episode", "notification_capability", "version"])
def test_forged_or_scoped_pushes_rejected(remote_harness, mutation):
    harness = remote_harness()
    value = invocation(harness)
    key = harness.runtimes[0].admission.seat_secret
    body = value.model_dump_json().encode()
    headers = signed_headers(body, value.turn_id, value.deadline.isoformat(), key)
    if mutation == "signature":
        headers["x-a2a-signature"] = "0" * 64
    elif mutation == "body":
        body = body.replace(b"test-push", b"test-forgery")
    elif mutation == "id":
        headers = signed_headers(body, "other", value.deadline.isoformat(), key)
    elif mutation == "deadline":
        headers = signed_headers(body, value.turn_id, (value.deadline + timedelta(seconds=1)).isoformat(), key)
    else:
        changes = {"expired": {"deadline": datetime.now(timezone.utc) - timedelta(seconds=1)},
                   "seat": {"seat": 1}, "episode": {"episode_id": "other"},
                   "notification_capability": {"capability": "not-authorized"},
                   "version": {"protocol_version": "a2a-turns/999"}}[mutation]
        value = value.model_copy(update=changes)
        body = value.model_dump_json().encode()
        headers = signed_headers(body, value.turn_id, value.deadline.isoformat(), key)
    assert push(harness, value, body=body, headers=headers).status_code == 401
    assert not harness.runtimes[0].registered
    assert not harness.invocations
    assert harness.episode.seats[0].recorder is None


def test_action_push_before_registration(remote_harness):
    harness = remote_harness()
    value = invocation(harness).model_copy(update={"kind": "turn", "phase": "DECISION", "capability": "anything"})
    assert push(harness, value).status_code == 409
    assert not harness.invocations


def test_bootstrap_ticket_is_not_valid_for_later_pushes(remote_harness):
    harness = remote_harness()
    assert push(harness, invocation(harness), key=harness.episode.seats[0].ticket).status_code == 401
    assert not harness.invocations


def test_concurrent_duplicate_push_executes_once(remote_harness):
    harness = remote_harness()
    value = invocation(harness)
    body = value.model_dump_json().encode()
    headers = signed_headers(body, value.turn_id, value.deadline.isoformat(), harness.runtimes[0].admission.seat_secret)

    async def send():
        async with httpx.AsyncClient(trust_env=False) as client:
            return await asyncio.gather(*[client.post(harness.callbacks[0].base_url + "/turns", content=body,
                                                    headers=headers) for _ in range(8)])

    responses = asyncio.run(send())
    assert all(response.status_code == 200 for response in responses)
    assert len(harness.invocations) == 1


@pytest.mark.parametrize("kind,phase,capability", [("turn", "DECISION_RETRY", "cap"), ("turn", "CHEAP_TALK", None)])
def test_invalid_action_invocations_fail_explicitly(remote_harness, kind, phase, capability):
    harness = remote_harness()
    value = invocation(harness)
    assert push(harness, value).status_code == 200
    value = value.model_copy(update={"turn_id": "unsupported", "kind": kind, "phase": phase, "capability": capability})
    assert push(harness, value).status_code == 400
    assert len(harness.invocations) == 1
