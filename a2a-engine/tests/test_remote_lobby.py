import asyncio
import json
import time

import pytest

from a2a_engine.remote.contract import PROTOCOL_VERSION, PROTOCOL_VERSION_V2
from a2a_engine.remote.dispatch import DeliveryFailure
from a2a_engine.remote.lobby import EpisodeSession, Registration, SeatsUnavailable, open_episode


PROTOCOLS = frozenset({PROTOCOL_VERSION, PROTOCOL_VERSION_V2})


def session_for(harness):
    return EpisodeSession({"num_agents": 2, "agents": [{"runtime": "external"}] * 2}, harness.context)


def test_local_handle_and_complete_roster():
    with open_episode(None, {"environment_id": "word_guess", "num_agents": 2}) as session:
        handle = json.loads(session.handle.to_json())
        assert handle["episode_id"] != handle["attempt_id"]
        assert handle["join_deadline"] is None
        assert all(s["join_url"] is None for s in handle["seats"])
        with pytest.raises(ValueError, match="every roster seat"):
            with session.started({0: Registration({})}, protocols=PROTOCOLS, register_timeout_s=1):
                pass
        with session.started({1: Registration({"role": "host"}), 0: Registration({})},
                             protocols=PROTOCOLS, register_timeout_s=1) as roster:
            assert [s.seat for s in roster.seats] == [1, 0]
            assert all(s.protocol_version is None for s in roster.seats)
    assert session.closed


@pytest.mark.parametrize("fixture", ["remote_harness", "remote_harness_v2"])
def test_barrier_dispatch_order_and_credential_free_handle(request, fixture):
    harness = request.getfixturevalue(fixture)(seats=(0, 1))
    session = session_for(harness)
    public = session.handle.to_json()
    for seat in harness.episode.seats.values():
        assert seat.ticket not in public and seat.secret not in public and seat.callback_url not in public
    assert "ticket" not in public and "callback" not in public
    with session.started({1: Registration({"game_config": {}}), 0: Registration({"game_config": {}})}, protocols=PROTOCOLS, register_timeout_s=2) as roster:
        assert [s.seat for s in roster.seats] == [1, 0]
        assert all(s.registered for s in harness.episode.seats.values())
    assert [(i.seat, i.kind) for i in harness.invocations] == [(1, "register"), (0, "register"), (1, "episode_end"), (0, "episode_end")]


def test_no_show_barrier_never_registers_ready_seat(remote_harness):
    harness = remote_harness(seats=(0, 1), join=False, join_timeout_s=.05)
    harness.episode.seats[0].ready.set()
    session = session_for(harness)
    start = time.monotonic()
    with pytest.raises(SeatsUnavailable) as error:
        with session.started({0: Registration({}), 1: Registration({})}, protocols=PROTOCOLS, register_timeout_s=1):
            pytest.fail("no-show entered play")
    assert error.value.seats == (1,)
    assert time.monotonic() - start < .5
    assert not harness.invocations


@pytest.mark.parametrize("failure", ["registration", "play", "unavailable", "cancelled"])
def test_teardown_only_registered_seats(remote_harness, monkeypatch, failure):
    harness = remote_harness(seats=(0, 1))
    session = session_for(harness)
    calls = []
    def dispatch(seat, kind, *args):
        calls.append((seat.seat, kind))
        if failure == "registration" and seat.seat == 1 and kind == "register":
            raise DeliveryFailure("unreachable", 1)
    monkeypatch.setattr(session, "_dispatch", dispatch)
    exception = {"registration": SeatsUnavailable, "play": RuntimeError,
                 "unavailable": SeatsUnavailable, "cancelled": asyncio.CancelledError}[failure]
    with pytest.raises(exception):
        with session.started({0: Registration({}), 1: Registration({})}, protocols=PROTOCOLS, register_timeout_s=1):
            if failure == "unavailable":
                raise SeatsUnavailable([1])
            raise exception()
    expected_end = [] if failure == "cancelled" else [(0, "episode_end")] + ([] if failure == "registration" else [(1, "episode_end")])
    assert calls == [(0, "register"), (1, "register"), *expected_end]


def test_protocol_requirement_and_inactive_episode(remote_harness):
    harness = remote_harness(seats=(0, 1))
    session = session_for(harness)
    for inactive in (False, True):
        harness.episode.active = not inactive
        with pytest.raises(SeatsUnavailable) as error:
            with session.started({0: Registration({}), 1: Registration({})},
                                 protocols=frozenset({PROTOCOL_VERSION_V2}), register_timeout_s=1):
                pass
        assert error.value.seats == (0, 1)
    assert not harness.invocations


def test_end_delivery_failure_does_not_skip_later_registered_seats(remote_harness, monkeypatch):
    harness = remote_harness(seats=(0, 1))
    session = session_for(harness)
    calls = []
    def dispatch(seat, kind, *args):
        calls.append((seat.seat, kind))
        if kind == "episode_end":
            raise DeliveryFailure("unreachable", 1)
    monkeypatch.setattr(session, "_dispatch", dispatch)
    with session.started({0: Registration({}), 1: Registration({})}, protocols=PROTOCOLS, register_timeout_s=1):
        pass
    assert calls == [(0, "register"), (1, "register"), (0, "episode_end"), (1, "episode_end")]


def test_local_attempt_does_not_provision_and_close_is_idempotent():
    from unittest.mock import Mock
    manager = Mock()
    session = open_episode(manager, {"num_agents": 2})
    manager.provision.assert_not_called()
    managed = Mock()
    session.managed = managed
    session.close()
    session.close()
    managed.close.assert_called_once()
