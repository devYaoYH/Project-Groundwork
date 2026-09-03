"""LLM-as-judge transcript prompt extraction.

`build_transcript_prompt(record)` returns OpenAI-format chat messages ready
to send to a judge model. `JudgeContext` exposes the structured rendering
inputs if callers want to use their own templates.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from a2a_engine.dataset import GameMessage, EpisodeRecord
from a2a_engine.schemas import ParticipantBinding

DEFAULT_SYSTEM_PROMPT = (
    "You are an expert analyst of multi-agent coordination transcripts. "
    "You will be shown a environment's full transcript, final state, and metrics. "
    "Identify the key coordination behaviors that explain the environment's outcome — "
    "successes, failures, missed opportunities, and notable communication patterns. "
    "Be specific, ground each observation in transcript turns by quoting briefly."
)


class JudgeContext(BaseModel):
    """Structured inputs used to render a judge prompt."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    episode_uid: str
    environment_id: str
    experiment_name: str | None
    agents: list[ParticipantBinding]
    messages: list[GameMessage]
    final_state: dict[str, Any]
    metrics: dict[str, Any]

    @classmethod
    def from_record(cls, record: EpisodeRecord) -> "JudgeContext":
        return cls(
            episode_uid=record.episode_uid,
            environment_id=record.environment_id,
            experiment_name=record.experiment_name,
            agents=record.agents,
            messages=record.messages,
            final_state=record.final_state,
            metrics=record.metrics,
        )


def _agent_label(idx: int, agent: ParticipantBinding) -> str:
    name = agent.extra.get("name") if agent.extra else None
    if not name:
        # Pull from any extra-allowed top-level attr, else fall back to index.
        name = getattr(agent, "name", None) or f"agent_{idx}"
    return f"  - {agent.type} {agent.model or '<unspecified>'} [name={name}]"


def render_transcript(ctx: JudgeContext, *, include_metrics: bool = True) -> str:
    lines: list[str] = []
    lines.append(f"GAME: {ctx.episode_uid}")
    lines.append(f"EXPERIMENT: {ctx.experiment_name or '<none>'}")
    lines.append(f"GAME TYPE: {ctx.environment_id}")
    lines.append("AGENTS:")
    for i, a in enumerate(ctx.agents):
        lines.append(_agent_label(i, a))
    lines.append("")
    lines.append("TRANSCRIPT:")
    for m in ctx.messages:
        lines.append(f"[turn {m.turn}] {m.speaker}: {m.text}")
    lines.append("")
    lines.append("FINAL STATE:")
    lines.append(json.dumps(ctx.final_state, indent=2, default=str))
    if include_metrics:
        lines.append("")
        lines.append("METRICS:")
        lines.append(json.dumps(ctx.metrics, indent=2, default=str))
    return "\n".join(lines)


def build_transcript_prompt(
    record: EpisodeRecord,
    *,
    system_prompt: str | None = None,
    include_metrics: bool = True,
) -> list[dict]:
    """Return OpenAI-format chat messages for an LLM-as-judge call."""
    ctx = JudgeContext.from_record(record)
    user = render_transcript(ctx, include_metrics=include_metrics)
    return [
        {"role": "system", "content": system_prompt or DEFAULT_SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]
