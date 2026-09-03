"""OpenTelemetry lifecycle spans for Calendar's existing client protocol.

Calendar predates the engine's smaller ``AgentInterface`` and has a richer
sync ``BaseClient`` contract.  This adapter keeps that contract intact while
making every client lifecycle transition a child of the engine-owned environment span.
It deliberately records only stable operational dimensions, never prompts,
messages, calendars, or model output.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from a2a_engine.tracing_otel import get_tracer

from calendar_game.agents import (
    BaseClient,
    DecideResult,
    GameConfig,
    ReflectionResult,
    TurnResult,
)


T = TypeVar("T")


class InstrumentedCalendarClient(BaseClient):
    """Delegate a ``BaseClient`` while emitting low-cardinality lifecycle spans."""

    def __init__(self, delegate: BaseClient, *, agent_id: int) -> None:
        self.delegate = delegate
        self.agent_id = agent_id

    def _call(self, operation: str, callback: Callable[[], T], **attributes: Any) -> T:
        tracer = get_tracer()
        with tracer.start_as_current_span(f"calendar.client.{operation}") as span:
            span.set_attribute("a2a.environment.name", "calendar")
            span.set_attribute("a2a.agent.id", self.agent_id)
            span.set_attribute("a2a.client.type", type(self.delegate).__name__)
            span.set_attribute("a2a.operation.name", operation)
            for key, value in attributes.items():
                if value is not None:
                    span.set_attribute(key, value)
            result = callback()
            latency_ms = getattr(result, "latency_ms", None)
            if latency_ms is not None:
                span.set_attribute("a2a.client.latency_ms", float(latency_ms))
            return result

    def register(self, agent_id: int, game_config: GameConfig) -> None:
        return self._call(
            "register",
            lambda: self.delegate.register(agent_id, game_config),
            **{"a2a.environment.num_agents": game_config.num_agents},
        )

    def start_round(self, meeting: dict, calendar_render: str, round_num: int) -> None:
        return self._call(
            "start_round",
            lambda: self.delegate.start_round(meeting, calendar_render, round_num),
            **{"a2a.round": round_num, "a2a.meeting.id": meeting.get("id")},
        )

    def turn(
        self,
        messages: list[dict],
        turn_index: int | None = None,
        max_turns_per_round: int | None = None,
    ) -> TurnResult:
        return self._call(
            "turn",
            lambda: self.delegate.turn(messages, turn_index, max_turns_per_round),
            **{
                "a2a.turn.index": turn_index,
                "a2a.turn.inbox_message_count": len(messages),
                "a2a.turn.max_per_round": max_turns_per_round,
            },
        )

    def decide(self, meeting: dict, calendar_render: str) -> DecideResult:
        return self._call(
            "decide",
            lambda: self.delegate.decide(meeting, calendar_render),
            **{"a2a.meeting.id": meeting.get("id")},
        )

    def retry_decide(self, attempt: int, max_attempts: int, conflict: str) -> DecideResult:
        return self._call(
            "retry_decide",
            lambda: self.delegate.retry_decide(attempt, max_attempts, conflict),
            **{"a2a.decision.retry": attempt, "a2a.decision.max_retries": max_attempts},
        )

    def voluntary_decide(self, meeting: dict, calendar_render: str) -> DecideResult:
        return self._call(
            "voluntary_decide",
            lambda: self.delegate.voluntary_decide(meeting, calendar_render),
            **{"a2a.meeting.id": meeting.get("id")},
        )

    def observe_calendar(self, calendar_render: str) -> None:
        return self._call("observe_calendar", lambda: self.delegate.observe_calendar(calendar_render))

    def observe_penalty(self, incurred_penalty: int) -> None:
        return self._call(
            "observe_penalty",
            lambda: self.delegate.observe_penalty(incurred_penalty),
        )

    def reflect_calendar_belief(
        self,
        target_agent_id: int,
        num_slots: int,
        round_num: int | None = None,
    ) -> ReflectionResult | None:
        return self._call(
            "reflect_calendar_belief",
            lambda: self.delegate.reflect_calendar_belief(target_agent_id, num_slots, round_num),
            **{
                "a2a.reflection.target_agent_id": target_agent_id,
                "a2a.round": round_num,
                "a2a.calendar.slot_count": num_slots,
            },
        )
