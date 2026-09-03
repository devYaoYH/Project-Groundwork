"""The durable event log, and the projection that reads it back.

The point of the sink is that it survives a process that never gets to run any
cleanup, so these tests care about what is on disk *between* two appends rather
than about what a tidy shutdown produces.
"""

from __future__ import annotations

import json

from a2a_engine.event_sink import (
    JsonlEventSink,
    current_event_sink,
    iter_event_sinks,
    open_event_sink,
    read_event_sink,
)
from a2a_engine.schemas import Event
from a2a_engine.stream_projection import project_events_to_trace, project_stream_to_trace
from a2a_engine.tracing import EventLog


def _sink(tmp_path, name="e1"):
    return JsonlEventSink(
        tmp_path / f"{name}.events.jsonl",
        episode_uid=name,
        episode_id="exp.cell.0",
        environment_id="word_guess",
    )


def test_every_appended_event_is_on_disk_before_append_returns(tmp_path):
    sink = _sink(tmp_path)
    log = EventLog(sink=sink)

    log.append("game_start", {"num_agents": 2})
    # No close, no flush, no cleanup: this is what a SIGKILL would leave.
    assert len(read_event_sink(sink.path)) == 1

    log.append("message", {"speaker": "a", "text": "hi"})
    assert len(read_event_sink(sink.path)) == 2


def test_a_truncated_final_line_is_skipped_rather_than_fatal(tmp_path):
    sink = _sink(tmp_path)
    log = EventLog(sink=sink)
    log.append("game_start", {"num_agents": 2})
    log.append("message", {"speaker": "a", "text": "hi"})
    # A process killed mid-write leaves half a line behind.
    with sink.path.open("a", encoding="utf-8") as handle:
        handle.write('{"episode_uid": "e1", "event": {"type": "mess')

    entries = read_event_sink(sink.path)
    assert [entry["event"]["type"] for entry in entries] == ["game_start", "message"]


def test_the_sink_records_the_identity_the_control_plane_joins_on(tmp_path):
    sink = _sink(tmp_path)
    EventLog(sink=sink).append("game_start", {})

    line = json.loads(sink.path.read_text().splitlines()[0])
    assert line["episode_uid"] == "e1"
    assert line["episode_id"] == "exp.cell.0"
    assert line["environment_id"] == "word_guess"


def test_both_projections_produce_the_same_trace_from_the_same_events(tmp_path):
    sink = _sink(tmp_path)
    log = EventLog(sink=sink)
    log.append("game_start", {"num_agents": 2, "episode_uid": "e1"})
    log.append("message", {"speaker": "a", "text": "hi"})
    log.append("game_end", {"won": True})
    log.all()

    entries = read_event_sink(sink.path)
    from_file = project_events_to_trace(sink.path)
    from_stream = project_stream_to_trace(
        [{**entry, "stream_id": f"{index}-0"} for index, entry in enumerate(entries)],
        stream="a2a:launch:x:episode:exp.cell.0",
    )

    ignore = {"observability"}
    assert from_file.model_dump(exclude=ignore) == from_stream.model_dump(exclude=ignore)
    # Only where the events came from differs; the partial marking does not.
    assert from_file.observability["source"] == "event_sink_projection"
    assert from_stream.observability["source"] == "redis_stream_projection"
    assert from_file.observability["partial"] == from_stream.observability["partial"] is False


def test_an_episode_without_a_terminal_event_projects_as_partial(tmp_path):
    sink = _sink(tmp_path)
    log = EventLog(sink=sink)
    log.append("game_start", {"num_agents": 2})
    log.append("message", {"speaker": "a", "text": "hi"})
    # No game_end: the process died here.

    trace = project_events_to_trace(sink.path)
    assert trace.stopped is True
    assert trace.observability["partial"] is True
    assert trace.observability["event_count"] == 2
    # Inventing a summary would read as a result rather than an absence.
    assert trace.final_state == {}
    assert trace.ended_at is None


def test_an_event_log_picks_up_the_runners_sink_from_the_context(tmp_path):
    """Environments construct their own ``EventLog`` and know nothing about
    where a run's artifacts live, so the runner hands the sink over out of
    band rather than through the config -- which would put a machine-specific
    absolute path into the research record."""
    sink = _sink(tmp_path)
    token = current_event_sink.set(sink)
    try:
        EventLog().append("game_start", {})
    finally:
        current_event_sink.reset(token)

    assert len(read_event_sink(sink.path)) == 1


def test_an_unwritable_results_directory_degrades_rather_than_failing_the_run(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    assert open_event_sink(blocked, experiment_name="e", episode_uid="e1") is None


def test_sinks_are_discoverable_under_the_experiment_they_belong_to(tmp_path):
    sink = open_event_sink(tmp_path, experiment_name="exp", episode_uid="e1")
    assert sink is not None
    EventLog(sink=sink).append("game_start", {})

    assert [path.name for path in iter_event_sinks(tmp_path, "exp")] == ["e1.events.jsonl"]
    assert list(iter_event_sinks(tmp_path)) == [sink.path]


def test_closing_twice_is_harmless(tmp_path):
    sink = _sink(tmp_path)
    log = EventLog(sink=sink)
    log.append("game_start", {})
    # The environment closes it via ``all()``; the runner closes it again in a
    # finally. Both must be safe, and neither may lose what is already written.
    log.all()
    sink.close()
    assert len(read_event_sink(sink.path)) == 1
