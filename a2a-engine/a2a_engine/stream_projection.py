"""Project a Redis event stream into a trace-shaped document.

A running episode has no persisted trace yet — the canonical record is written
once, at the end. Until then the only evidence is the event stream, and a
researcher watching a rollout wants to see the game, not wait for it.

This module turns whatever a stream currently holds into the same shape a game
viewer already consumes, so one viewer renders a finished episode and a
half-finished one alike. A projection is never mistaken for the canonical
record: an episode that has not reached a terminal event is marked
``stopped`` and carries ``partial: True`` in its observability block.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from a2a_engine.schemas import GameConfigBase, GameEvent, GameTraceBase

# Games name their last event differently; each is the point after which no
# further events are expected.
TERMINAL_EVENT_TYPES = frozenset({"game_end", "game_complete", "game_stopped"})

_START_EVENT_TYPES = frozenset({"game_start"})


def _first_value(entries: list[dict[str, Any]], key: str) -> Any:
    return next((entry.get(key) for entry in entries if entry.get(key)), None)


def _start_payload(events: list[GameEvent]) -> dict[str, Any]:
    for event in events:
        if event.type in _START_EVENT_TYPES:
            return event.data or {}
    return {}


def _config_from_start(game_name: str, payload: dict[str, Any]) -> GameConfigBase:
    """Recover the run config a game announced when it started.

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
    fields.setdefault("game_name", game_name)
    fields.setdefault("num_agents", int(fields.get("num_agents") or 0))
    return GameConfigBase.model_validate(fields)


def project_stream_to_trace(
    entries: list[dict[str, Any]],
    *,
    stream: str,
    game_name: str | None = None,
) -> GameTraceBase:
    """Build a ``GameTraceBase`` from decoded stream entries.

    ``entries`` are the dicts returned by ``decode_stream_events``. Raises
    ``ValueError`` when the stream holds no usable events or no game identity.
    """
    if not entries:
        raise ValueError(f"stream {stream!r} contains no A2A game events")

    resolved_game = game_name or _first_value(entries, "game_name")
    if not resolved_game:
        raise ValueError(f"stream {stream!r} has no game_name; pass one explicitly")

    events = [GameEvent.model_validate(entry["event"]) for entry in entries]
    episode_id = _first_value(entries, "episode_id")
    start_payload = _start_payload(events)

    last = events[-1]
    complete = last.type in TERMINAL_EVENT_TYPES
    # A game's terminal event carries its own summary; mid-flight there is
    # simply nothing to summarise yet, and inventing zeros would read as a
    # result rather than an absence.
    final_state: dict[str, Any] = dict(last.data or {}) if complete else {}

    config = _config_from_start(resolved_game, start_payload)
    config.experiment_run_id = config.experiment_run_id or episode_id

    return GameTraceBase(
        game_id=str(start_payload.get("game_id") or episode_id or stream),
        config=config,
        events=events,
        final_state=final_state,
        metrics=final_state,
        observability={
            "source": "redis_stream_projection",
            "redis_stream": stream,
            "partial": not complete,
            "event_count": len(events),
        },
        started_at=events[0].timestamp,
        ended_at=last.timestamp if complete else None,
        stopped=not complete,
    )


def projection_summary(trace: GameTraceBase) -> dict[str, Any]:
    """The few facts a viewer needs to caption a projection honestly."""
    return {
        "partial": bool(trace.observability.get("partial")),
        "event_count": len(trace.events),
        "last_event_type": trace.events[-1].type if trace.events else None,
        "generated_at": datetime.now(UTC).isoformat(),
    }
