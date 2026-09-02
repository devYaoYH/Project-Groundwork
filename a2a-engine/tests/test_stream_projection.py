"""Seam tests: Redis stream -> trace-shaped projection.

The point of the projection is that a viewer can render an episode that has
not finished. The risk is that a half-finished episode is presented as a
completed result, so partial-ness is pinned here as hard as the happy path.
"""

import pytest

from a2a_engine.stream_projection import project_stream_to_trace


def entry(event_type, data=None, *, game="calendar", episode="expt.batch.0"):
    return {
        "game_name": game,
        "episode_id": episode,
        "stream_id": "1-0",
        "event": {"type": event_type, "timestamp": "2026-08-31T12:00:00Z", "data": data or {}},
    }


def test_completed_stream_projects_a_finished_trace():
    entries = [
        entry("game_start", {"num_agents": 5, "game_id": "cal-1"}),
        entry("dm_sent", {"from": 0}),
        entry("game_end", {"coordination_rate": 0.8}),
    ]

    trace = project_stream_to_trace(entries, stream="s")

    assert trace.stopped is False
    assert trace.observability["partial"] is False
    assert trace.ended_at is not None
    assert trace.final_state == {"coordination_rate": 0.8}
    assert trace.game_id == "cal-1"


def test_in_flight_stream_is_marked_partial_and_carries_no_result():
    """A rollout being watched live has no summary yet; showing zeros would
    read as a result rather than an absence."""
    entries = [
        entry("game_start", {"num_agents": 5}),
        entry("dm_sent", {"from": 0}),
    ]

    trace = project_stream_to_trace(entries, stream="s")

    assert trace.stopped is True
    assert trace.observability["partial"] is True
    assert trace.ended_at is None
    assert trace.final_state == {}
    assert trace.metrics == {}
    assert len(trace.events) == 2


@pytest.mark.parametrize("terminal", ["game_end", "game_complete", "game_stopped"])
def test_each_game_s_terminal_event_completes_the_projection(terminal):
    """Calendar ends with game_end, negotiation with game_complete. A game
    whose terminal type is unrecognised would look permanently in-flight."""
    entries = [entry("game_start", {}), entry(terminal, {"score": 1})]

    assert project_stream_to_trace(entries, stream="s").stopped is False


def test_nested_config_is_recovered_when_a_game_publishes_one():
    entries = [entry("game_start", {"config": {"game_name": "negotiation", "num_agents": 2,
                                               "num_rounds": 4}})]

    config = project_stream_to_trace(entries, stream="s", game_name="negotiation").config

    assert config.game_name == "negotiation"
    assert config.num_agents == 2
    assert config.model_dump()["num_rounds"] == 4


def test_flat_start_payload_still_yields_a_usable_config():
    """Calendar flattens its public fields into game_start instead."""
    entries = [entry("game_start", {"num_agents": 5, "num_slots": 8, "difficulty": "easy"})]

    config = project_stream_to_trace(entries, stream="s").config

    assert config.game_name == "calendar"
    assert config.num_agents == 5


def test_episode_id_survives_into_the_config():
    entries = [entry("game_start", {}, episode="calendar_smoke.pinned.0")]

    trace = project_stream_to_trace(entries, stream="s")

    assert trace.config.experiment_run_id == "calendar_smoke.pinned.0"


def test_empty_and_anonymous_streams_are_refused():
    with pytest.raises(ValueError, match="no A2A game events"):
        project_stream_to_trace([], stream="s")

    anonymous = [{"event": {"type": "game_start", "timestamp": "2026-08-31T12:00:00Z", "data": {}}}]
    with pytest.raises(ValueError, match="no game_name"):
        project_stream_to_trace(anonymous, stream="s")
