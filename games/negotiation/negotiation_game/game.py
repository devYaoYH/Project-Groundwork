"""NegotiationGame — the a2a-engine plugin wrapper around the existing engine.

This is an *adapter*, not a reimplementation. All environment logic stays in
``negotiation_game.backend.engine.GameEngine``, which was already headless: it
takes a resolved ``GameConfig`` plus two agent implementations and exposes
``on_event(callback)`` / ``await run_game()``. The only things this module adds are:

1. a ``EpisodeConfigBase`` subclass so experiment YAML validates,
2. an event adapter turning engine events into ``Event``s (with cheap talk
   normalized to the ``{speaker, text}`` shape the judge and ``EpisodeDataset``
   expect), and
3. the sync ``run()`` the runner contract wants, wrapping ``asyncio.run``.

The previous HTTP path (``scripts/run_experiment.py`` POSTing to a live server
and polling ``/api/cell/status``) is gone: the runner now executes games
in-process like every other environment.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from pydantic import Field

from a2a_engine import EpisodeConfigBase, Event, EpisodeTrace, register_environment
from a2a_engine.redis_stream import publisher_from_config

from negotiation_game.backend.agents import make_agent
from negotiation_game.backend.engine import GameConfig, GameEngine, GameMode
from negotiation_game.backend.defaults import (
    DEFAULT_AGENT_BUDGET,
    DEFAULT_CHEAP_TALK_TURNS,
    DEFAULT_MAX_RESOURCE_TYPES_PER_TURN,
    DEFAULT_NUM_ROUNDS,
    DEFAULT_PROJECTS_A,
    DEFAULT_PROJECTS_B,
    DEFAULT_RESOURCE_COSTS,
    DEFAULT_RESOURCE_SUPPLY,
    DEFAULT_RESOURCE_TYPES,
)

#: Engine event types that carry natural-language utterances. These are
#: re-emitted with ``{speaker, text}`` so ``to_messages_df()`` and the judge's
#: transcript builder work on negotiation episodes without environment-specific code.
MESSAGE_EVENT_TYPES = {"cheap_talk", "message", "talk_turn"}


class NegotiationConfig(EpisodeConfigBase):
    """Experiment-facing config. Mirrors ``backend.engine.GameConfig`` fields."""

    environment_id: str = "negotiation"
    num_agents: int = 2

    # --- economy ---
    resource_types: list[str] = Field(default_factory=lambda: list(DEFAULT_RESOURCE_TYPES))
    resource_supply: dict[str, int] = Field(default_factory=lambda: dict(DEFAULT_RESOURCE_SUPPLY))
    resource_costs: dict[str, float] = Field(default_factory=lambda: dict(DEFAULT_RESOURCE_COSTS))
    agent_budget: float = DEFAULT_AGENT_BUDGET
    max_resource_types_per_turn: int = DEFAULT_MAX_RESOURCE_TYPES_PER_TURN

    # --- structure ---
    mode: str = "stable"
    num_rounds: int = DEFAULT_NUM_ROUNDS
    cheap_talk_turns: int = DEFAULT_CHEAP_TALK_TURNS
    enable_cheap_talk: bool = True
    first_speaker: int = 0
    swapped: bool = False

    # --- scenario (may be synthesized by resolve_config) ---
    agent_projects: list[list[dict]] = Field(
        default_factory=lambda: [list(DEFAULT_PROJECTS_A), list(DEFAULT_PROJECTS_B)]
    )
    oracle_stats: dict | None = None
    scenario_synergy: dict | None = None
    agent_shifting: list[bool] = Field(default_factory=lambda: [False, False])
    scenario_pool_path: str | None = None
    mc_ratio: float | None = None
    target_mc_ratio: float | None = None
    rotate_projects: bool = False
    named_projects: bool = False

    # --- information / framing conditions ---
    goal: str = ""
    thinking: bool = True
    visible_utilities: bool = False
    visible_opponent_reward: bool = True
    share_projects: bool = False
    think_about_opponent: bool = False
    maximize_joint: bool = False
    full_transparency: bool = False


def _to_engine_config(cfg: NegotiationConfig) -> GameConfig:
    """Project the pydantic config onto the engine's dataclass config.

    Field names are shared by construction, so this stays a mechanical copy of
    the intersection rather than a hand-maintained mapping.
    """
    payload = cfg.model_dump()
    payload["mode"] = GameMode(payload.get("mode", "stable"))
    valid = {f for f in GameConfig.__dataclass_fields__}
    return GameConfig(**{k: v for k, v in payload.items() if k in valid})


#: Fields ``GameConfig.__post_init__`` overwrites unconditionally. They are not
#: settable knobs: the engine always shows an agent its own projects and always
#: hides the opponent's reward. Listing them here keeps the trace honest instead
#: of recording a requested value the environment ignored.
ENGINE_PINNED_FIELDS = ("visible_utilities", "visible_opponent_reward")


def _sync_effective_config(cfg: NegotiationConfig, engine_cfg: GameConfig) -> None:
    """Write engine-decided values back onto the trace config."""
    for field in ENGINE_PINNED_FIELDS:
        setattr(cfg, field, getattr(engine_cfg, field))


class NegotiationGame:
    """Runs one negotiation environment and returns a ``EpisodeTrace``."""

    def __init__(self, config: dict, dry_run: bool = False) -> None:
        self.config = NegotiationConfig(**config)
        self.dry_run = dry_run
        self.events: list[Event] = []
        self._publisher = publisher_from_config(self.config)

    # --- event adaptation ---

    def _record(self, event_type: str, data: dict) -> None:
        """Translate one engine event into a Event.

        Cheap-talk events get a ``{speaker, text}`` projection layered on top of
        their original payload; nothing is dropped, so environment-specific analysis
        that reads the raw fields keeps working.
        """
        payload = dict(data)
        if event_type in MESSAGE_EVENT_TYPES:
            speaker = payload.get("speaker") or payload.get("agent") or payload.get("agent_id")
            text = payload.get("text") or payload.get("message") or payload.get("content")
            if speaker is not None and text is not None:
                payload.setdefault("speaker", speaker)
                payload.setdefault("text", text)
        event = Event(type=event_type, timestamp=datetime.now(timezone.utc), data=payload)
        self.events.append(event)
        if self._publisher is not None:
            self._publisher.publish(event)

    def _make_agents(self) -> tuple[Any, Any]:
        """Build agent implementations, ordered by first_speaker.

        Dry runs substitute heuristic agents, which need no API keys — the same
        stand-in the previous runner used for ``--dry-run``.
        """
        specs = [dict(a) if isinstance(a, dict) else a.model_dump()
                 for a in self.config.agents] or [{"type": "heuristic"}, {"type": "heuristic"}]
        if self.dry_run:
            specs = [{"type": "heuristic"} for _ in specs]
        impls = [make_agent(spec) for spec in specs]
        fs = self.config.first_speaker
        return impls[fs], impls[1 - fs]

    # --- runner contract ---

    def run(self) -> EpisodeTrace:
        started = datetime.now(timezone.utc)
        engine_config = _to_engine_config(self.config)
        # The engine's __post_init__ pins some fields regardless of what was
        # requested. Copy the effective values back so the trace records the
        # config the environment was actually played under, not the one asked for.
        _sync_effective_config(self.config, engine_config)
        agent_a, agent_b = self._make_agents()

        engine = GameEngine(engine_config, agent_a, agent_b)
        engine.on_event(self._on_engine_event)

        result = asyncio.run(engine.run_game())
        ended = datetime.now(timezone.utc)
        if self._publisher is not None:
            self._publisher.flush()

        return EpisodeTrace(
            episode_uid="",  # the runner assigns this
            config=self.config,
            events=self.events,
            final_state=result,
            metrics=_metrics_from_result(result),
            started_at=started,
            ended_at=ended,
            stopped=bool(result.get("stopped", False)),
        )

    async def _on_engine_event(self, event_type: str, data: dict) -> None:
        """Engine callbacks are async and called as ``cb(event_type, data)``."""
        self._record(event_type, data or {})


def _metrics_from_result(result: dict) -> dict[str, Any]:
    """Lift headline numbers out of the engine's result dict.

    Kept deliberately small: ``final_state`` already holds the full result, so
    ``metrics`` only carries what cross-environment comparison and leaderboards need.
    """
    rounds = result.get("rounds", []) or []
    a_total = result.get("agent_a_cumulative_reward")
    b_total = result.get("agent_b_cumulative_reward")
    metrics: dict[str, Any] = {
        "num_rounds": len(rounds),
        "agent_a_total_reward": a_total,
        "agent_b_total_reward": b_total,
        "overdrawn_rounds": sum(1 for r in rounds if r.get("overdrawn")),
    }
    if a_total is not None and b_total is not None:
        joint = a_total + b_total
        metrics["joint_reward"] = joint
        best, worst = max(a_total, b_total), min(a_total, b_total)
        # Fairness as min/max, matching the framework's convention. Undefined
        # when the leader scored nothing, so report None rather than 0.
        metrics["fairness"] = (worst / best) if best else None
        oracle = (result.get("oracle_stats") or {}).get("max_joint_reward")
        if oracle:
            metrics["efficiency"] = joint / oracle
    return metrics
