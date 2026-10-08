import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from a2a_engine.remote.contract import PROTOCOL_VERSION
from a2a_engine.remote.dispatch import loopback_url


def join_body(harness, **overrides):
    return {"join_ticket": harness.episode.seats[0].ticket,
            "callback_url": harness.callbacks[0].base_url, "protocol_versions": [PROTOCOL_VERSION],
            "agent_info": {"name": "reference", "seat_secret": "do-not-persist"}, **overrides}


def post_join(harness, body):
    return httpx.post(f"{harness.io.base_url}/episodes/{harness.episode.episode_id}/join", json=body, trust_env=False)


def test_challenge_and_ready_barrier(remote_harness):
    harness = remote_harness(join=False)
    response = post_join(harness, join_body(harness))
    assert response.status_code == 200
    seat = harness.episode.seats[0]
    assert not seat.ready.is_set()
    assert not seat.registered
    assert seat.agent_info == {"name": "reference"}
    data = response.json()
    assert data["seat_secret"] != seat.ticket
    assert data["mcp"] == {name: f"{harness.io.base_url}/episodes/episode/{name}/mcp" for name in ("env", "comm")}
    assert not harness.invocations
    assert httpx.post(data["ready_url"], headers={"authorization": "Bearer wrong"}, trust_env=False).status_code == 401
    assert httpx.post(data["ready_url"], headers={"authorization": f"Bearer {data['seat_secret']}"}, trust_env=False).status_code == 200
    assert harness.episode.wait_ready(0) is seat
    assert post_join(harness, join_body(harness)).status_code == 400


@pytest.mark.parametrize("url", ["http://localhost:80", "http://example.com:80", "http://192.168.1.2:80",
                                  "https://127.0.0.1:80", "http://user:pass@127.0.0.1:80", "http://127.0.0.1:80?q=x",
                                  "http://127.0.0.1:80#fragment", "http://127.0.0.1", "file:///etc/passwd"])
def test_unsafe_callbacks_rejected_without_consuming_ticket(remote_harness, url):
    harness = remote_harness(join=False)
    assert post_join(harness, join_body(harness, callback_url=url)).status_code == 400
    assert not harness.episode.seats[0].consumed
    assert not harness.episode.seats[0].secret
    with pytest.raises(ValueError):
        loopback_url(url)


@pytest.mark.parametrize("changes", [{"protocol_versions": ["a2a-turns/999"]}, {"join_ticket": "x" * 43},
                                     {"agent_info": {"name": "x" * 257}}, {"seat": 1}])
def test_invalid_admission(remote_harness, changes):
    harness = remote_harness(join=False)
    assert post_join(harness, join_body(harness, **changes)).status_code == 400
    assert not harness.episode.seats[0].ready.is_set()
    assert not harness.episode.seats[0].secret


def test_expired_ticket_and_bounded_no_show(remote_harness):
    harness = remote_harness(join=False, join_timeout_s=0.05)
    time.sleep(0.06)
    assert post_join(harness, join_body(harness)).status_code == 400
    with pytest.raises(TimeoutError, match="seat_unavailable"):
        harness.episode.wait_ready(0)


def test_host_origin_and_body_guards(remote_harness):
    harness = remote_harness(join=False)
    url = f"{harness.io.base_url}/episodes/episode/join"
    for headers in ({"host": "evil.example"}, {"origin": "http://evil.example"}):
        assert httpx.post(url, json=join_body(harness), headers=headers, trust_env=False).status_code == 403
    assert httpx.post(url, content=b"x" * 262145, trust_env=False).status_code == 413
    assert not harness.episode.seats[0].consumed


def test_redirect_callback_is_not_followed(remote_harness):
    from starlette.responses import RedirectResponse
    from starlette.routing import request_response
    harness = remote_harness(join=False)

    async def redirect(request):
        return RedirectResponse("http://example.com/steal", status_code=307)

    harness.runtimes[0].app.routes[0].endpoint = redirect
    harness.runtimes[0].app.routes[0].app = request_response(redirect)
    assert post_join(harness, join_body(harness)).status_code == 400
    assert harness.episode.seats[0].consumed
    assert harness.episode.seats[0].secret is None


def test_concurrent_join_consumes_ticket_once(remote_harness):
    harness = remote_harness(join=False)
    body = join_body(harness)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: post_join(harness, body), range(4)))
    assert sorted(response.status_code for response in responses) == [200, 400, 400, 400]
    assert not harness.episode.seats[0].ready.is_set()
