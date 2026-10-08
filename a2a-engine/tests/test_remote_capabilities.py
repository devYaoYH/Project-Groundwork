import base64
import json

import pytest

from a2a_engine.remote.capabilities import AuthorizationError, Capabilities
from a2a_engine.remote.contract import CapabilityClaims, contract_schemas


def claims(**overrides):
    return CapabilityClaims(**{"episode_id": "ep", "attempt_id": "attempt", "seat": 0,
                              "turn_id": "turn", "phase": "DECISION", "allowed_tools": ["env.schedule"],
                              "exp": 100, **overrides})


def test_mint_verify_revoke():
    capabilities = Capabilities(clock=lambda: 50)
    claim = claims()
    token = capabilities.mint(claim)
    assert capabilities.verify(token, episode_id="ep", attempt_id="attempt") == claim
    with pytest.raises(AuthorizationError):
        capabilities.mint(claims(turn_id="another"))
    capabilities.revoke(claim)
    with pytest.raises(AuthorizationError):
        capabilities.verify(token, episode_id="ep", attempt_id="attempt")


@pytest.mark.parametrize("mutation", ["signature", "seat", "turn", "phase", "episode", "expiry", "tools"])
def test_claim_tampering(mutation):
    capabilities = Capabilities(clock=lambda: 50)
    token = capabilities.mint(claims())
    body, signature = token.split(".")
    values = json.loads(base64.urlsafe_b64decode(body))
    if mutation == "signature":
        signature = "0" * 64
    else:
        key = {"episode": "episode_id", "turn": "turn_id", "expiry": "exp", "tools": "allowed_tools"}.get(mutation, mutation)
        values[key] = 1 if mutation == "seat" else 999 if mutation == "expiry" else ["env.reschedule"] if mutation == "tools" else "forged"
        body = base64.urlsafe_b64encode(json.dumps(values).encode()).decode()
    with pytest.raises(AuthorizationError):
        capabilities.verify(f"{body}.{signature}", episode_id="ep", attempt_id="attempt")


@pytest.mark.parametrize("episode,attempt,now", [("wrong", "attempt", 50), ("ep", "wrong", 50), ("ep", "attempt", 100)])
def test_scope_and_expiry(episode, attempt, now):
    clock = [50]
    capabilities = Capabilities(clock=lambda: clock[0])
    token = capabilities.mint(claims())
    clock[0] = now
    with pytest.raises(AuthorizationError):
        capabilities.verify(token, episode_id=episode, attempt_id=attempt)


def test_stale_revocation_does_not_revoke_later_turn():
    capabilities = Capabilities(clock=lambda: 50)
    first = claims()
    capabilities.mint(first)
    capabilities.revoke(first)
    second = claims(turn_id="new")
    token = capabilities.mint(second)
    capabilities.revoke(first)
    assert capabilities.verify(token, episode_id="ep", attempt_id="attempt") == second


def test_contract_has_no_completion_actions_or_lifecycle_tool():
    schemas = contract_schemas()
    assert schemas["TurnCompletion"]["additionalProperties"] is False
    assert "actions" not in schemas["TurnCompletion"]["properties"]
    assert schemas["TurnInvocation"]["properties"]["protocol_version"]["const"] == "a2a-turns/1"
