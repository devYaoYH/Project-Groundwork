"""RQ11 — LLM Token Usage Analysis for V5+ Project Games.

Extracts actual API-reported token usage from api_meta fields in game traces,
with per-round granularity as the primary unit of analysis.

Falls back to character-based estimation when api_meta is not available.

Usage:
    uv run python -m scripts.analysis.rq11_token_usage
    uv run python -m scripts.analysis.rq11_token_usage --run-id a1b2c3d4
"""

import argparse
import logging
from pathlib import Path
from typing import Union, Any

import numpy as np
import pandas as pd

from negotiation_analysis.data_loader import filter_games, load_experiment_data

log = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4  # fallback estimate when api_meta unavailable


def _resolve_model(agent_id: str, agents: list[dict]) -> str:
    """Map agent_id (agent_a/agent_b) to model name from game config."""
    idx = 0 if agent_id == "agent_a" else 1
    if idx < len(agents):
        agent_cfg = agents[idx]
        return agent_cfg.get("model", agent_cfg.get("type", "unknown"))
    return "unknown"


def _extract_api_meta_from_events(events: list[dict]) -> dict[tuple[str, int, int], dict]:
    """Extract api_meta from thinking/reasoning events, keyed by (agent, round, turn)."""
    meta_by_key: dict[tuple[str, int, int], dict] = {}
    for event in events:
        etype = event.get("type", "")
        data = event.get("data", {})
        if etype in ("thinking", "reasoning") and "api_meta" in data:
            key = (data.get("agent", ""), data.get("round", 0), data.get("turn", -1))
            existing = meta_by_key.get(key, {})
            meta = data["api_meta"]
            # Accumulate tokens across thinking + reasoning events for same turn
            for field in ("prompt_tokens", "completion_tokens", "total_tokens",
                          "reasoning_tokens", "cached_prompt_tokens",
                          "cache_creation_input_tokens", "cache_read_input_tokens"):
                val = meta.get(field)
                if val is not None:
                    existing[field] = existing.get(field, 0) + val
            existing["duration_s"] = meta.get("duration_s", existing.get("duration_s", 0))
            meta_by_key[key] = existing
    return meta_by_key


def _convert_negotiation_dataset_to_raw_games(dataset: Any) -> list[dict]:
    """Convert NegotiationDataset to raw game format for analyze_token_usage.

    This avoids the slow Firestore load and works with the already-loaded dataset.
    """
    raw_games = []
    for game in dataset.games:
        raw_game = {
            "game_id": game.game_id,
            "experiment_label": game.label,
            "agents": game.config.get("agents", []),
            "events": game.all_events,
            "rounds": [r.data for r in game.rounds],
        }
        raw_games.append(raw_game)
    return raw_games


def _extract_api_meta_from_transcript(rounds: list[dict]) -> dict[tuple[str, int, int], dict]:
    """Extract api_meta from transcript entries in round data."""
    meta_by_key: dict[tuple[str, int, int], dict] = {}
    for r in rounds:
        round_num = r.get("round_number", 0)
        transcript = r.get("cheap_talk_transcript", [])
        for entry in transcript:
            if "api_meta" not in entry:
                continue
            speaker = entry.get("speaker", "")
            turn = entry.get("turn", -1)
            key = (speaker, round_num, turn)
            existing = meta_by_key.get(key, {})
            meta = entry["api_meta"]
            for field in ("prompt_tokens", "completion_tokens", "total_tokens",
                          "reasoning_tokens", "cached_prompt_tokens",
                          "cache_creation_input_tokens", "cache_read_input_tokens"):
                val = meta.get(field)
                if val is not None:
                    existing[field] = existing.get(field, 0) + val
            existing["duration_s"] = meta.get("duration_s", existing.get("duration_s", 0))
            meta_by_key[key] = existing
    return meta_by_key


def analyze_token_usage(games_input: Any) -> dict:
    """Analyze token usage across games at per-round granularity.

    Accepts either:
    - List of raw game dicts (legacy format from Firestore)
    - NegotiationDataset (fast path using already-loaded dataset)

    Prefers actual API-reported token counts from api_meta. Falls back to
    character-based estimates when api_meta is unavailable.

    Returns:
        Dict with 'model_summary', 'per_round', 'per_game', 'model_goal_summary' DataFrames.
    """
    # Convert NegotiationDataset to raw game format if needed (duck typing for flexibility)
    if hasattr(games_input, 'games') and not isinstance(games_input, list):
        raw_games = _convert_negotiation_dataset_to_raw_games(games_input)
    else:
        raw_games = games_input

    per_round_rows = []

    for game in raw_games:
        game_id = game["game_id"]
        agents = game.get("agents", [])
        events = game.get("events", [])
        rounds = game.get("rounds", [])
        label = game.get("experiment_label", "")

        if not events and not rounds:
            continue

        # Build api_meta lookup from events and transcript
        meta_from_events = _extract_api_meta_from_events(events)
        meta_from_transcript = _extract_api_meta_from_transcript(rounds)

        # Character-based stats from events (fallback)
        round_chars: dict[tuple[str, int], dict] = {}

        for event in events:
            etype = event.get("type", "")
            data = event.get("data", {})

            if etype == "thinking":
                agent_id = data.get("agent", "")
                round_num = data.get("round", 0)
                chars = len(data.get("content", ""))
                key = (agent_id, round_num)
                if key not in round_chars:
                    round_chars[key] = {"speech_chars": 0, "thinking_chars": 0,
                                        "reasoning_chars": 0, "speech_count": 0,
                                        "thinking_count": 0}
                round_chars[key]["thinking_chars"] += chars
                round_chars[key]["thinking_count"] += 1

            elif etype == "reasoning":
                agent_id = data.get("agent", "")
                round_num = data.get("round", 0)
                chars = len(data.get("content", ""))
                key = (agent_id, round_num)
                if key not in round_chars:
                    round_chars[key] = {"speech_chars": 0, "thinking_chars": 0,
                                        "reasoning_chars": 0, "speech_count": 0,
                                        "thinking_count": 0}
                round_chars[key]["reasoning_chars"] += chars

            elif etype == "cheap_talk":
                if data.get("type", "speech") == "system_notice":
                    continue
                agent_id = data.get("speaker", "")
                round_num = data.get("round", 0)
                chars = len(data.get("message", ""))
                key = (agent_id, round_num)
                if key not in round_chars:
                    round_chars[key] = {"speech_chars": 0, "thinking_chars": 0,
                                        "reasoning_chars": 0, "speech_count": 0,
                                        "thinking_count": 0}
                round_chars[key]["speech_chars"] += chars
                round_chars[key]["speech_count"] += 1

        # Aggregate api_meta per (agent, round) — sum across turns
        round_api_meta: dict[tuple[str, int], dict] = {}
        for (agent_id, round_num, _turn), meta in {**meta_from_transcript, **meta_from_events}.items():
            key = (agent_id, round_num)
            existing = round_api_meta.get(key, {})
            for field in ("prompt_tokens", "completion_tokens", "total_tokens",
                          "reasoning_tokens", "cached_prompt_tokens",
                          "cache_creation_input_tokens", "cache_read_input_tokens"):
                val = meta.get(field)
                if val is not None:
                    existing[field] = existing.get(field, 0) + val
            existing["duration_s"] = existing.get("duration_s", 0) + meta.get("duration_s", 0)
            round_api_meta[key] = existing

        # Build per-round rows
        all_keys = set(round_chars.keys()) | set(round_api_meta.keys())
        for (agent_id, round_num) in all_keys:
            model = _resolve_model(agent_id, agents)
            chars = round_chars.get((agent_id, round_num), {
                "speech_chars": 0, "thinking_chars": 0, "reasoning_chars": 0,
                "speech_count": 0, "thinking_count": 0,
            })
            api = round_api_meta.get((agent_id, round_num), {})

            output_chars = chars["speech_chars"] + chars["thinking_chars"] + chars["reasoning_chars"]

            row = {
                "game_id": game_id,
                "experiment_label": label,
                "model": model,
                "agent_id": agent_id,
                "round_number": round_num,
                # Character-based (always available)
                "speech_chars": chars["speech_chars"],
                "thinking_chars": chars["thinking_chars"],
                "reasoning_chars": chars["reasoning_chars"],
                "speech_count": chars["speech_count"],
                "thinking_count": chars["thinking_count"],
                "output_chars": output_chars,
                "est_output_tokens": output_chars / CHARS_PER_TOKEN,
                # API-reported (may be None)
                "prompt_tokens": api.get("prompt_tokens"),
                "completion_tokens": api.get("completion_tokens"),
                "reasoning_tokens": api.get("reasoning_tokens"),
                "total_tokens": api.get("total_tokens"),
                "cached_prompt_tokens": api.get("cached_prompt_tokens"),
                "duration_s": api.get("duration_s"),
                # Derived: visible output tokens = completion - reasoning (for reasoning models)
                "visible_output_tokens": None,
            }
            if row["completion_tokens"] is not None and row["reasoning_tokens"] is not None:
                row["visible_output_tokens"] = row["completion_tokens"] - row["reasoning_tokens"]

            per_round_rows.append(row)

    per_round_df = pd.DataFrame(per_round_rows)

    if per_round_df.empty:
        return {
            "model_summary": pd.DataFrame(),
            "per_round": per_round_df,
            "per_game": pd.DataFrame(),
        }

    # Determine if we have real API data
    has_api_data = per_round_df["completion_tokens"].notna().any()

    # Model summary
    def _model_agg(group):
        result = {
            "games": group["game_id"].nunique(),
            "agent_rounds": len(group),
            "speech_count": group["speech_count"].sum(),
            "thinking_count": group["thinking_count"].sum(),
            "avg_speech_chars": group["speech_chars"].sum() / max(group["speech_count"].sum(), 1),
            "avg_thinking_chars": group["thinking_chars"].sum() / max(group["thinking_count"].sum(), 1),
            "avg_est_output_tokens": group["est_output_tokens"].mean(),
        }
        if has_api_data:
            api_rows = group[group["completion_tokens"].notna()]
            if not api_rows.empty:
                result["avg_prompt_tokens"] = api_rows["prompt_tokens"].mean()
                result["avg_completion_tokens"] = api_rows["completion_tokens"].mean()
                result["avg_reasoning_tokens"] = api_rows["reasoning_tokens"].mean()
                result["avg_visible_output_tokens"] = api_rows["visible_output_tokens"].mean()
                result["avg_cached_prompt_tokens"] = api_rows["cached_prompt_tokens"].mean()
                result["avg_total_tokens"] = api_rows["total_tokens"].mean()
                result["avg_duration_s"] = api_rows["duration_s"].mean()
                result["reasoning_fraction"] = (
                    api_rows["reasoning_tokens"].sum() / max(api_rows["completion_tokens"].sum(), 1)
                )
                result["cache_hit_rate"] = (
                    api_rows["cached_prompt_tokens"].sum() / max(api_rows["prompt_tokens"].sum(), 1)
                )
                result["api_coverage"] = len(api_rows) / len(group)
        return pd.Series(result)

    model_summary_df = per_round_df.groupby("model").apply(_model_agg, include_groups=False).reset_index()

    # Per-game summary
    per_game_df = (
        per_round_df.groupby(["game_id", "model", "agent_id"])
        .agg(
            num_rounds=("round_number", "nunique"),
            speech_chars=("speech_chars", "sum"),
            thinking_chars=("thinking_chars", "sum"),
            reasoning_chars=("reasoning_chars", "sum"),
            est_output_tokens=("est_output_tokens", "sum"),
            prompt_tokens=("prompt_tokens", "sum"),
            completion_tokens=("completion_tokens", "sum"),
            reasoning_tokens=("reasoning_tokens", "sum"),
            total_tokens=("total_tokens", "sum"),
        )
        .reset_index()
    )

    return {
        "model_summary": model_summary_df,
        "per_round": per_round_df,
        "per_game": per_game_df,
        "has_api_data": has_api_data,
    }


def print_summary(results: dict) -> None:
    """Print a formatted summary of token usage analysis."""
    summary = results["model_summary"]
    per_round = results["per_round"]
    has_api = results.get("has_api_data", False)

    if summary.empty:
        print("No data with events found.")
        return

    print(f"\n{'='*100}")
    print("RQ11: LLM Token Usage Analysis")
    print(f"{'='*100}")
    print(f"Games analyzed: {per_round['game_id'].nunique()}")
    print(f"Total agent-rounds: {len(per_round)}")
    print(f"API token data available: {'Yes' if has_api else 'No (char-based estimates only)'}")
    print()

    # Character-based summary (always available)
    print("Character-Based Output (always available):")
    print("-" * 80)
    char_cols = ["model", "games", "agent_rounds", "avg_speech_chars",
                 "avg_thinking_chars", "avg_est_output_tokens"]
    available = [c for c in char_cols if c in summary.columns]
    print(summary[available].to_string(index=False, float_format="%.0f"))

    if has_api:
        print()
        print("API-Reported Token Usage:")
        print("-" * 100)
        api_cols = ["model", "avg_prompt_tokens", "avg_completion_tokens",
                    "avg_reasoning_tokens", "avg_visible_output_tokens",
                    "avg_total_tokens", "reasoning_fraction", "cache_hit_rate",
                    "avg_duration_s", "api_coverage"]
        available = [c for c in api_cols if c in summary.columns]
        if available:
            print(summary[available].to_string(index=False, float_format="%.1f"))

    # Per-round trend
    print()
    print("Per-round trends (mean across all models):")
    print("-" * 80)
    trend_agg = {"est_output_tokens": "mean", "game_id": "count"}
    if has_api:
        for col in ["completion_tokens", "reasoning_tokens", "prompt_tokens"]:
            if col in per_round.columns and per_round[col].notna().any():
                trend_agg[col] = "mean"
    round_agg = per_round.groupby("round_number").agg(**{
        k if k != "game_id" else "n": pd.NamedAgg(column=k, aggfunc=v)
        for k, v in trend_agg.items()
    }).reset_index()
    print(round_agg.to_string(index=False, float_format="%.0f"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze LLM token usage")
    parser.add_argument("--run-id", nargs="+", help="Experiment run ID prefix(es)")
    parser.add_argument("--min-schema", type=int, default=5, help="Minimum schema version")
    parser.add_argument("--model", nargs="+", help="Model name(s) to include")
    parser.add_argument("--label", nargs="+", help="Experiment label substring(s)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    all_games = load_experiment_data()
    games = filter_games(
        all_games,
        run_ids=args.run_id,
        min_schema_version=args.min_schema,
        models=args.model,
        labels=args.label,
    )
    print(f"Filtered: {len(games)}/{len(all_games)} games")
    results = analyze_token_usage(games)
    print_summary(results)
