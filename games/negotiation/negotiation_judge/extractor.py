"""Extract judge contexts from NegotiationDataset.

Supports both per-round contexts (legacy) and per-game contexts (current).
The current pipeline uses per-game: one LLM call per game sees all rounds
so the judge can identify cross-round dynamics (repair, learning, regression).
"""

import logging
from typing import Optional

from negotiation_analysis.models import NegotiationDataset, NegotiationGame, NegotiationRound
from negotiation_judge.schema import (
    CheapTalkTurn,
    JudgeGameContext,
    JudgeRoundContext,
    RoundData,
    RoundOutcome,
)

log = logging.getLogger("judge.extractor")

THINKING_TRUNCATE_CHARS = 1500  # ~500 tokens


def _derive_round_outcome(rnd: NegotiationRound) -> RoundOutcome:
    if rnd.overdrawn:
        return RoundOutcome.overdrawn
    if rnd.is_optimal:
        return RoundOutcome.optimal
    return RoundOutcome.suboptimal


def _is_decision_turn(t) -> bool:
    """Return True if this turn contains a decision (early or final) event."""
    return any(e.type in ("decision", "early_decision") for e in t.events)


def _extract_cheap_talk(rnd: NegotiationRound, truncate_thinking: int = THINKING_TRUNCATE_CHARS) -> list[CheapTalkTurn]:
    """Build CheapTalkTurn list from round turns (agent turns only).

    Speech attached to a decision turn is omitted: the engine appends that
    speech to the transcript AFTER the allocation is submitted, so the opponent
    never saw it.  Including it would let the judge attribute distrust or
    non-response behaviors to speech the opponent simply never received.
    """
    agent_turns = [t for t in rnd.turns if t.speaker in ("agent_a", "agent_b")]

    turns = []
    for t in agent_turns:
        thinking_text = None
        if t.thinking:
            thinking_text = t.thinking.content
            if truncate_thinking and len(thinking_text) > truncate_thinking:
                thinking_text = thinking_text[:truncate_thinking] + " [truncated]"
        speech_text = t.speech.content if t.speech else None
        is_decision = _is_decision_turn(t) or t.turn_number == -1
        if speech_text and is_decision:
            speech_text = None  # this agent's speech was attached to their decision; not visible to opponent
        if thinking_text or speech_text:
            turns.append(CheapTalkTurn(
                speaker=t.speaker,
                turn=t.turn_number,
                thinking=thinking_text,
                speech=speech_text,
                is_decision=is_decision,
            ))
    return turns


def _safe_efficiency(rnd: NegotiationRound) -> float:
    """Return joint_efficiency or 0.0 if NaN."""
    eff = rnd.joint_efficiency
    return eff if eff == eff else 0.0  # NaN check


def _extract_round_data(rnd: NegotiationRound) -> RoundData:
    """Build a RoundData object (for game-level context)."""
    return RoundData(
        round_number=rnd.round_number,
        round_outcome=_derive_round_outcome(rnd),
        joint_efficiency=_safe_efficiency(rnd),
        reward_a=rnd.agent_a_reward,
        reward_b=rnd.agent_b_reward,
        cheap_talk=_extract_cheap_talk(rnd),
        allocation_a=rnd.agent_a_resources,
        allocation_b=rnd.agent_b_resources,
        auto_allocated_a=rnd.agent_a_auto_allocated,
        auto_allocated_b=rnd.agent_b_auto_allocated,
    )


def extract_round_context(game: NegotiationGame, rnd: NegotiationRound) -> JudgeRoundContext:
    """Build a JudgeRoundContext from a single game round (legacy per-round flow)."""
    oracle = rnd.oracle_stats or {}
    return JudgeRoundContext(
        game_id=game.game_id,
        round_number=rnd.round_number,
        model_a=game.model_a,
        model_b=game.model_b,
        mode=game.mode,
        mc_ratio=oracle.get("mc_ratio"),
        oracle_optimum=rnd.collab_max or None,
        optimal_allocation=oracle.get("collab_detail"),
        round_outcome=_derive_round_outcome(rnd),
        joint_efficiency=_safe_efficiency(rnd),
        reward_a=rnd.agent_a_reward,
        reward_b=rnd.agent_b_reward,
        cheap_talk=_extract_cheap_talk(rnd),
        allocation_a=rnd.agent_a_resources,
        allocation_b=rnd.agent_b_resources,
        auto_allocated_a=rnd.agent_a_auto_allocated,
        auto_allocated_b=rnd.agent_b_auto_allocated,
    )


def _derive_shifting_agent(game: NegotiationGame) -> Optional[str]:
    """Return 'agent_a', 'agent_b', or None for stable games."""
    if not game.is_shifting:
        return None
    if game.agent_a_shifted:
        return "agent_a"
    if game.agent_b_shifted:
        return "agent_b"
    return None


def extract_game_context(game: NegotiationGame) -> JudgeGameContext:
    """Build a JudgeGameContext from a whole game (all rounds)."""
    # Use game-level oracle for non-rotating games; per-round oracle is still
    # available in NegotiationRound.oracle_stats if needed. For the prompt
    # header we just show the game-level summary.
    oracle = game.oracle_stats or {}
    sorted_rounds = sorted(game.rounds, key=lambda r: r.round_number)
    return JudgeGameContext(
        game_id=game.game_id,
        model_a=game.model_a,
        model_b=game.model_b,
        mode=game.mode,
        shifting_agent=_derive_shifting_agent(game),
        mc_ratio=oracle.get("mc_ratio"),
        oracle_optimum=oracle.get("collab_max"),
        optimal_allocation=oracle.get("collab_detail"),
        rounds=[_extract_round_data(r) for r in sorted_rounds],
    )


def extract_all_contexts(
    dataset: NegotiationDataset,
    min_schema_version: int = 5,
    skip_baselines: bool = True,
    game_ids: Optional[set[str]] = None,
) -> list[JudgeRoundContext]:
    """Extract per-round contexts (legacy). Returns one entry per round."""
    contexts = []
    skipped_version = 0
    skipped_baseline = 0

    for game in dataset.games:
        if game_ids and game.game_id not in game_ids:
            continue
        if game.schema_version < min_schema_version:
            skipped_version += 1
            continue
        if skip_baselines and game.is_baseline:
            skipped_baseline += 1
            continue

        for rnd in game.rounds:
            contexts.append(extract_round_context(game, rnd))

    if skipped_version:
        log.warning("Skipped %d games with schema_version < %d", skipped_version, min_schema_version)
    if skipped_baseline:
        log.warning("Skipped %d baseline games (no cheap talk)", skipped_baseline)

    log.info("Extracted %d round contexts from %d games",
             len(contexts), len(dataset.games) - skipped_version - skipped_baseline)
    return contexts


def extract_all_game_contexts(
    dataset: NegotiationDataset,
    min_schema_version: int = 5,
    skip_baselines: bool = True,
    game_ids: Optional[set[str]] = None,
) -> list[JudgeGameContext]:
    """Extract per-game contexts. Returns one entry per game (with all rounds)."""
    contexts = []
    skipped_version = 0
    skipped_baseline = 0
    skipped_empty = 0

    for game in dataset.games:
        if game_ids and game.game_id not in game_ids:
            continue
        if game.schema_version < min_schema_version:
            skipped_version += 1
            continue
        if skip_baselines and game.is_baseline:
            skipped_baseline += 1
            continue
        if not game.rounds:
            skipped_empty += 1
            continue

        contexts.append(extract_game_context(game))

    if skipped_version:
        log.warning("Skipped %d games with schema_version < %d", skipped_version, min_schema_version)
    if skipped_baseline:
        log.warning("Skipped %d baseline games (no cheap talk)", skipped_baseline)
    if skipped_empty:
        log.warning("Skipped %d games with no rounds", skipped_empty)

    total_rounds = sum(len(c.rounds) for c in contexts)
    log.info("Extracted %d game contexts (%d total rounds)", len(contexts), total_rounds)
    return contexts
