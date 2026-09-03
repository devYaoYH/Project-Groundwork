"""RQ17: Agent Error & Fallback Rate.

Reports the frequency of agent errors that result in heuristic fallbacks,
malformed outputs, validation failures, and auto-filled decisions. These
indicate cases where agents fail to follow instructed parameters.

Error event types tracked:
    - validation_error: Agent output failed schema validation (e.g. >2 resource types, malformed JSON)
    - output_format_warning: Agent submitted project runs without resource purchases (auto-corrected)
    - decision_auto_filled: Engine inferred resource purchases from project runs
    - api_failure: LLM API call failed (rate limit, timeout, returned None/length cutoff)
    - heuristic_fallback: LLM response unusable, fell back to heuristic agent for speech or decision

Usage:
    uv run python -m scripts.analysis.rq17_agent_errors
"""

import numpy as np
import argparse
import pandas as pd

from negotiation_analysis.data_loader import add_common_args, load_dataset_from_args
from negotiation_analysis.models import NegotiationDataset

ERROR_EVENT_TYPES = {
    "validation_error",
    "output_format_warning",
    "decision_auto_filled",
    "api_failure",
    "heuristic_fallback",
}


def analyze_agent_errors(dataset: NegotiationDataset) -> dict:
    """Analyze agent error and fallback rates across games.

    Returns dict with:
        - error_df: one row per error event with context
        - game_df: per-environment error counts
        - by_model: error rates per model
        - summary: aggregate stats
    """
    error_records = []
    game_records = []

    for g in dataset.games:
        if g.schema_version < 5:
            continue
        model_map = {"agent_a": g.model_a, "agent_b": g.model_b}
        episode_uid = g.episode_uid
        num_rounds = g.num_rounds
        events = g.all_events

        game_error_counts = {t: 0 for t in ERROR_EVENT_TYPES}
        game_errors_by_agent = {"agent_a": 0, "agent_b": 0}

        for e in events:
            etype = e.get("type") or e.get("event_type", "")
            if etype not in ERROR_EVENT_TYPES:
                continue
            data = e.get("data", {})
            agent = data.get("agent", "unknown")
            model = model_map.get(agent, "unknown")

            error_records.append({
                "episode_uid": episode_uid,
                "event_type": etype,
                "agent": agent,
                "model": model,
                "error": data.get("error", ""),
                "warning": data.get("warning", ""),
                "reason": data.get("reason", ""),
                "detail": data,
            })

            game_error_counts[etype] += 1
            if agent in game_errors_by_agent:
                game_errors_by_agent[agent] += 1

        total_errors = sum(game_error_counts.values())
        game_records.append({
            "episode_uid": episode_uid,
            "model_a": g.model_a,
            "model_b": g.model_b,
            "pair": g.pair_name,
            "num_rounds": num_rounds,
            "total_errors": total_errors,
            "has_errors": total_errors > 0,
            "agent_a_errors": game_errors_by_agent["agent_a"],
            "agent_b_errors": game_errors_by_agent["agent_b"],
            **{f"n_{t}": game_error_counts[t] for t in ERROR_EVENT_TYPES},
        })

    error_df = pd.DataFrame(error_records)
    game_df = pd.DataFrame(game_records)

    if game_df.empty:
        return {"error_df": error_df, "game_df": game_df,
                "by_model": pd.DataFrame(), "samples": [], "summary": {}}

    # Per-model error rate (melt: each environment → 2 agent rows)
    model_rows = []
    for _, row in game_df.iterrows():
        model_rows.append({
            "model": row["model_a"], "errors": row["agent_a_errors"],
            "rounds": row["num_rounds"], "episode_uid": row["episode_uid"],
        })
        model_rows.append({
            "model": row["model_b"], "errors": row["agent_b_errors"],
            "rounds": row["num_rounds"], "episode_uid": row["episode_uid"],
        })
    model_melt = pd.DataFrame(model_rows)
    by_model = model_melt.groupby("model").agg(
        n_games=pd.NamedAgg(column="episode_uid", aggfunc="count"),
        total_errors=pd.NamedAgg(column="errors", aggfunc="sum"),
        total_rounds=pd.NamedAgg(column="rounds", aggfunc="sum"),
        games_with_errors=pd.NamedAgg(column="errors", aggfunc=lambda x: (x > 0).sum()),
    )
    by_model["error_rate_per_round"] = (by_model["total_errors"] / by_model["total_rounds"]).round(4)
    by_model["pct_games_with_errors"] = (by_model["games_with_errors"] / by_model["n_games"]).round(3)

    # Per-model x error-type breakdown
    if not error_df.empty:
        by_model_type = (
            error_df.groupby(["model", "event_type"])
            .size()
            .unstack(fill_value=0)
        )
        # Ensure all error types present as columns
        for t in ERROR_EVENT_TYPES:
            if t not in by_model_type.columns:
                by_model_type[t] = 0
        by_model = by_model.join(by_model_type, how="left").fillna(0)
        for t in ERROR_EVENT_TYPES:
            by_model[t] = by_model[t].astype(int)

    # Error type breakdown
    error_type_counts = {}
    if not error_df.empty:
        error_type_counts = error_df["event_type"].value_counts().to_dict()

    # Collect samples with context
    samples = []
    if not error_df.empty:
        for _, row in error_df.iterrows():
            samples.append({
                "episode_uid": row["episode_uid"],
                "event_type": row["event_type"],
                "agent": row["agent"],
                "model": row["model"],
                "error": row["error"],
                "warning": row["warning"],
                "reason": row["reason"],
            })

    n_games = len(game_df)
    summary = {
        "n_games": n_games,
        "games_with_errors": int(game_df["has_errors"].sum()),
        "pct_games_with_errors": game_df["has_errors"].mean(),
        "total_errors": int(game_df["total_errors"].sum()),
        "error_rate_per_game": game_df["total_errors"].mean(),
        "error_type_counts": error_type_counts,
    }

    return {
        "error_df": error_df,
        "game_df": game_df,
        "by_model": by_model,
        "samples": samples,
        "summary": summary,
    }


def print_summary(results: dict) -> None:
    print("=" * 60)
    print("RQ17: Agent Error & Fallback Rate")
    print("=" * 60)

    s = results["summary"]
    print(f"\nGames analyzed: {s['n_games']}")
    print(f"Games with errors: {s['games_with_errors']} ({s['pct_games_with_errors']:.1%})")
    print(f"Total error events: {s['total_errors']}")
    print(f"Error rate per environment: {s['error_rate_per_game']:.2f}")

    if s["error_type_counts"]:
        print("\nBy error type:")
        for etype, count in sorted(s["error_type_counts"].items(), key=lambda x: -x[1]):
            print(f"  {etype:<30} {count:>5}")

    by_model = results["by_model"]
    if not by_model.empty:
        print("\nBy model:")
        print(by_model.to_string())

    samples = results.get("samples", [])
    if samples:
        print(f"\nSample errors ({min(len(samples), 10)}/{len(samples)}):")
        for s in samples[:10]:
            msg = s["error"] or s["warning"] or s["reason"]
            print(f"  {s['episode_uid'][:8]} [{s['model']}] {s['event_type']}: {msg[:120]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RQ17: Agent Errors")
    add_common_args(parser)
    args = parser.parse_args()
    dataset = load_dataset_from_args(args)
    results = analyze_agent_errors(dataset)
    print_summary(results)