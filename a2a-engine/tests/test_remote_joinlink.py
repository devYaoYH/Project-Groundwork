import base64
import json
import secrets
import time

import httpx
import pytest

from a2a_engine.remote.joinlink import decode_join_link, encode_join_link


def descriptor():
    return {"join_url": "http://127.0.0.1:1234/episodes/test/join", "seat": 0,
            "environment_id": "calendar", "expires_at": time.time() + 60,
            "join_ticket": secrets.token_urlsafe(32)}


def raw(value):
    return "a2a-join:" + base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def test_round_trip():
    value = descriptor()
    assert decode_join_link(encode_join_link(value)) == value


@pytest.mark.parametrize("link", ["", "a2a-join:", "a2a-join:abcd=", "a2a-join:ab+c",
    "a2a-join:" + "a" * 8192, raw('{"seat":0,"seat":1}'), raw("[]"), raw("{}"),
    "a2a-join:Zh", raw("[" * 2000)])
def test_malformed(link):
    with pytest.raises(ValueError, match="^invalid join link$"):
        decode_join_link(link)


@pytest.mark.parametrize("changes", [{"seat": True}, {"seat": -1}, {"expires_at": float("nan")},
    {"expires_at": True}, {"join_ticket": "short"}, {"environment_id": []},
    {"join_url": "http://user:secret@localhost/episodes/test/join"}, {"extra": 1}])
def test_strict_fields(changes):
    with pytest.raises(ValueError, match="^invalid join link$"):
        decode_join_link(raw(json.dumps({**descriptor(), **changes})))


@pytest.mark.parametrize("expired", [False, True])
def test_expired_and_consumed_links_rejected(remote_harness, expired):
    harness = remote_harness(join=False)
    seat = harness.episode.seats[0]
    if expired:
        seat.join_deadline = time.time() - 1
    value = descriptor()
    value.update(join_url=f"{harness.io.base_url}/episodes/{harness.episode.episode_id}/join",
                 join_ticket=seat.ticket, expires_at=seat.join_deadline)
    decoded = decode_join_link(encode_join_link(value))
    body = {"join_ticket": decoded["join_ticket"], "callback_url": harness.callbacks[0].base_url,
            "protocol_versions": ["a2a-turns/1"]}
    response = httpx.post(decoded["join_url"], json=body, trust_env=False)
    assert response.status_code == (400 if expired else 200)
    rejected = response if expired else httpx.post(decoded["join_url"], json=body, trust_env=False)
    assert rejected.json()["code"] == "admission_rejected"
