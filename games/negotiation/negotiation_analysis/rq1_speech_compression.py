"""RQ1: Speech Token Compression Over Rounds.

Do we observe decreasing speech token count as rounds/turns progress
(like common ground accumulation and context compression in humans)?
Compare stable vs shifting conditions. Account for early round termination.

Usage:
    uv run python scripts/analysis/rq1_speech_compression.py
"""

import numpy as np
import argparse
import pandas as pd
from scipy import stats

from negotiation_analysis.data_loader import add_common_args, load_dataset


def analyze_speech_compression(dataset: NegotiationDataset) -> dict:
    """Compute speech compression metrics across rounds.

    Returns a dict with DataFrames for plotting and summary statistics.
    """
    round_df = dataset.to_round_df()
    turn_df = dataset.to_turn_df()

    multi_round_ids = set(
        round_df[round_df["num_game_rounds"] >= 2]["game_id"]
    )
    mr_rounds = round_df[round_df["game_id"].isin(multi_round_ids)].copy()
    mr_turns = turn_df[turn_df["game_id"].isin(multi_round_ids)].copy()

    # Speech volume by round × mode
    speech_by_round_mode = (
        mr_rounds.groupby(["is_shifting", "round_number"])
        .agg(
            mean_speech_chars=pd.NamedAgg(
                column="total_speech_chars", aggfunc="mean"
            ),
            sem_speech_chars=pd.NamedAgg(
                column="total_speech_chars", aggfunc="sem"
            ),
            n_rounds=pd.NamedAgg(column="game_id", aggfunc="count"),
            n_games=pd.NamedAgg(column="game_id", aggfunc="nunique"),
        )
        .reset_index()
    )

    # Per-turn message length by round (stable only, most data)
    stable_turns = mr_turns[mr_turns["is_shifting"] == False]
    msg_len_by_turn_round = (
        stable_turns.groupby(["round_number", "turn_number"])
        .agg(
            mean_msg_len=pd.NamedAgg(column="message_len", aggfunc="mean"),
            sem_msg_len=pd.NamedAgg(column="message_len", aggfunc="sem"),
            n=pd.NamedAgg(column="message_len", aggfunc="count"),
        )
        .reset_index()
    )

    # Mean message length per round by shifting status
    msg_per_round = (
        mr_turns.groupby(["game_id", "round_number", "is_shifting"])
        .agg(mean_msg_len=pd.NamedAgg(column="message_len", aggfunc="mean"))
        .reset_index()
    )
    msg_by_round_cond = (
        msg_per_round.groupby(["is_shifting", "round_number"])
        .agg(
            mean=pd.NamedAgg(column="mean_msg_len", aggfunc="mean"),
            sem=pd.NamedAgg(column="mean_msg_len", aggfunc="sem"),
            n_games=pd.NamedAgg(column="game_id", aggfunc="nunique"),
        )
        .reset_index()
    )

    # Spearman per agent per mode
    agent_spearman = {}
    for (speaker, is_shifting), grp in mr_turns.groupby(["speaker", "is_shifting"]):
        if speaker == "system": continue
        if grp["round_number"].nunique() < 2:
            continue
        r, p = stats.spearmanr(grp["round_number"], grp["message_len"])
        mode_str = "shifting" if is_shifting else "stable"
        agent_spearman[f"{speaker}_{mode_str}"] = {
            "speaker": speaker, "is_shifting": is_shifting,
            "r": r, "p": p, "n": len(grp),
        }

    # Spearman correlation: speech chars ~ round number
    spearman_results = {}
    for is_shifting in [False, True]:
        mode_data = mr_rounds[mr_rounds["is_shifting"] == is_shifting]
        if mode_data["round_number"].nunique() < 2:
            continue
        r, p = stats.spearmanr(
            mode_data["round_number"], mode_data["total_speech_chars"]
        )
        mode_str = "shifting" if is_shifting else "stable"
        spearman_results[mode_str] = {"r": r, "p": p, "n": len(mode_data)}

    return {
        "speech_by_round_mode": speech_by_round_mode,
        "msg_len_by_turn_round": msg_len_by_turn_round,
        "msg_by_round_cond": msg_by_round_cond,
        "spearman_results": spearman_results,
        "multi_round_rounds": mr_rounds,
        "multi_round_turns": mr_turns,
        "agent_spearman": agent_spearman,
    }


def print_summary(results: dict) -> None:
    """Print a text summary of RQ1 results."""
    print("=" * 60)
    print("RQ1: Speech Token Compression Over Rounds")
    print("=" * 60)

    print("\nSpeech volume by round × shifting status:")
    print(
        results["speech_by_round_mode"][
            ["is_shifting", "round_number", "mean_speech_chars", "n_games"]
        ].to_string(index=False)
    )

    print("\nSpearman correlation (speech chars ~ round):")
    for mode, res in results["spearman_results"].items():
        print(f"  {mode}: r={res['r']:.3f}, p={res['p']:.4f} (n={res['n']})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RQ1: Speech Token Compression Over Rounds")
    add_common_args(parser)
    args = parser.parse_args()
    from negotiation_analysis.data_loader import load_dataset
    dataset = load_dataset(schema_version=args.schema_version, run_id=args.run_id)
    results = analyze_speech_compression(dataset)
    print_summary(results)