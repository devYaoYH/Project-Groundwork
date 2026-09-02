"""Durable Calendar fields consumed outside the live game process.

This is intentionally separate from the runtime loop: a persisted trace is the
handoff between the live LLM session and later replay, rating, or analysis.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


CALENDAR_RATING_CONTEXT_SCHEMA_VERSION = 1


def build_calendar_rating_context(scenario: Mapping[str, Any]) -> dict[str, Any]:
    """Return the self-contained, immutable-by-convention rating input block."""
    return {
        "schema_version": CALENDAR_RATING_CONTEXT_SCHEMA_VERSION,
        "calendars": deepcopy(scenario.get("calendars", [])),
        "meetings": deepcopy(scenario.get("meetings", [])),
        "prior_meetings": deepcopy(scenario.get("prior_meetings", [])),
        "task_id": scenario.get("task_id"),
    }
