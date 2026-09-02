from __future__ import annotations

from contextlib import contextmanager

from calendar_game.agents import CapturingClient, GameConfig
from calendar_game.observability import InstrumentedCalendarClient


class _Span:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value


class _Tracer:
    def __init__(self) -> None:
        self.spans: list[_Span] = []

    @contextmanager
    def start_as_current_span(self, name: str):
        span = _Span(name)
        self.spans.append(span)
        yield span


def test_client_lifecycle_wrapper_delegates_without_recording_content(monkeypatch):
    tracer = _Tracer()
    monkeypatch.setattr("calendar_game.observability.get_tracer", lambda: tracer)
    client = CapturingClient()
    wrapped = InstrumentedCalendarClient(client, agent_id=3)
    config = GameConfig(num_agents=4, num_slots=8, agent_id=3, all_agent_ids=[0, 1, 2, 3])

    wrapped.register(3, config)
    wrapped.start_round({"id": 9}, "private calendar contents", 2)
    wrapped.turn([{"content": "private message"}], turn_index=1, max_turns_per_round=4)
    wrapped.decide({"id": 9}, "private calendar contents")

    assert [span.name for span in tracer.spans] == [
        "calendar.client.register",
        "calendar.client.start_round",
        "calendar.client.turn",
        "calendar.client.decide",
    ]
    assert tracer.spans[2].attributes["a2a.turn.inbox_message_count"] == 1
    assert "private message" not in tracer.spans[2].attributes.values()
    assert "private calendar contents" not in tracer.spans[1].attributes.values()
    assert len(client.calls) == 4
