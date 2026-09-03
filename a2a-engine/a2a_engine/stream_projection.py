"""Project an event log into a trace-shaped document.

A running episode has no persisted trace yet — the canonical record is written
once, at the end. Until then the only evidence is the event log, and a
researcher watching a launch wants to see the environment, not wait for it.

Two sources feed the same projection. The durable local event sink is written
as the episode plays and is what makes an interrupted run recoverable with no
Redis configured; the optional Redis stream is the low-latency copy of the same
events. Both land in the shape a environment viewer already consumes, so one viewer
renders a finished episode and a half-finished one alike.

A projection is never mistaken for the canonical record: an episode that has
not reached a terminal event is marked ``stopped`` and carries ``partial: True``
in its observability block.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from a2a_engine.event_sink import read_event_sink
from a2a_engine.schemas import EpisodeConfigBase, Event, EpisodeTrace

# Games name their last event differently; each is the point after which no
# further events are expected.
TERMINAL_EVENT_TYPES = frozenset({"game_end", "game_complete", "game_stopped"})

_START_EVENT_TYPES = frozenset({"game_start"})


def _first_value(entries: list[dict[str, Any]], key: str) -> Any:
    return next((entry.get(key) for entry in entries if entry.get(key)), None)


def _start_payload(events: list[Event]) -> dict[str, Any]:
    for event in events:
        if event.type in _START_EVENT_TYPES:
            return event.data or {}
    return {}


def _config_from_start(environment_id: str, payload: dict[str, Any]) -> EpisodeConfigBase:
    """Recover the run config a environment announced when it started.

    Some games publish a nested ``config`` object; others flatten the fields
    they consider public into the start event. Both are accepted, and neither
    is required — a viewer that reads the events can render without it.
    """
    declared = payload.get("config")
    if isinstance(declared, dict):
        fields = dict(declared)
    else:
        fields = {
            key: payload[key]
            for key in ("num_agents", "agents", "seed", "num_slots")
            if key in payload
        }
    fields.setdefault("environment_id", environment_id)
    fields.setdefault("num_agents", int(fields.get("num_agents") or 0))
    return EpisodeConfigBase.model_validate(fields)


def _project(
    entries: list[dict[str, Any]],
    *,
    origin: str,
    source: str,
    origin_key: str,
    environment_id: str | None = None,
) -> EpisodeTrace:
    """The projection rules, shared by both sources.

    Only the two observability fields naming where the events came from differ
    between a Redis stream and a local event sink; the trace they produce from
    the same events is otherwise identical, including how a missing terminal
    event is marked.
    """
    if not entries:
        raise ValueError(f"{origin!r} contains no A2A environment events")

    resolved_game = environment_id or _first_value(entries, "environment_id")
    if not resolved_game:
        raise ValueError(f"{origin!r} has no environment_id; pass one explicitly")

    events = [Event.model_validate(entry["event"]) for entry in entries]
    episode_id = _first_value(entries, "episode_id")
    episode_uid = _first_value(entries, "episode_uid")
    start_payload = _start_payload(events)

    last = events[-1]
    complete = last.type in TERMINAL_EVENT_TYPES
    # A environment's terminal event carries its own summary; mid-flight there is
    # simply nothing to summarise yet, and inventing zeros would read as a
    # result rather than an absence.
    final_state: dict[str, Any] = dict(last.data or {}) if complete else {}

    config = _config_from_start(resolved_game, start_payload)
    config.episode_id = config.episode_id or episode_id

    return EpisodeTrace(
        episode_uid=str(
            start_payload.get("episode_uid") or episode_uid or episode_id or origin
        ),
        config=config,
        events=events,
        final_state=final_state,
        metrics=final_state,
        observability={
            "source": source,
            origin_key: origin,
            "partial": not complete,
            "event_count": len(events),
        },
        started_at=events[0].timestamp,
        ended_at=last.timestamp if complete else None,
        stopped=not complete,
    )


def project_stream_to_trace(
    entries: list[dict[str, Any]],
    *,
    stream: str,
    environment_id: str | None = None,
) -> EpisodeTrace:
    """Build a ``EpisodeTrace`` from decoded stream entries.

    ``entries`` are the dicts returned by ``decode_stream_events``. Raises
    ``ValueError`` when the stream holds no usable events or no environment identity.
    """
    return _project(
        entries,
        origin=stream,
        source="redis_stream_projection",
        origin_key="redis_stream",
        environment_id=environment_id,
    )


def project_events_to_trace(
    path: str | Path, *, environment_id: str | None = None
) -> EpisodeTrace:
    """Build a ``EpisodeTrace`` from one episode's durable event sink.

    This is what turns a ``SIGKILL``ed episode into evidence: the file was
    flushed event by event as it played, so whatever it holds is real, and a
    missing terminal event marks the result partial rather than absent.
    """
    return _project(
        read_event_sink(path),
        origin=str(path),
        source="event_sink_projection",
        origin_key="event_sink",
        environment_id=environment_id,
    )


def projection_summary(trace: EpisodeTrace) -> dict[str, Any]:
    """The few facts a viewer needs to caption a projection honestly."""
    return {
        "partial": bool(trace.observability.get("partial")),
        "event_count": len(trace.events),
        "last_event_type": trace.events[-1].type if trace.events else None,
        "generated_at": datetime.now(UTC).isoformat(),
    }
