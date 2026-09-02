"""RQ13: Strategy Anchoring & Persistence

Measures whether agents change strategy after a round, conditioned on whether
the previous round was an overdraw (annulment) or a success.

Usage:
    uv run python -m scripts.analysis.rq13_anchoring
"""

import numpy as np
import argparse
import pandas as pd

from negotiation_analysis.data_loader import add_common_args, load_dataset_from_args
from negotiation_analysis.models import NegotiationDataset



def analyze_anchoring(dataset: NegotiationDataset) -> pd.DataFrame:
    """One row per consecutive round pair (game-level, not per-agent).

    Draws allocation columns from NegotiationDataset.to_round_df() so that
    alloc_same_as_prev and joint_reward_improved stay in sync with the SQL
    rounds table. Only includes non-rotating games.
    """
    round_df = dataset.to_round_df()
    # Drop first-round rows (no previous) and rotating games
    pairs = round_df[
        round_df["alloc_same_as_prev"].notna() & ~round_df["is_rotating"]
    ].copy()

    if pairs.empty:
        return pd.DataFrame()

    # Bring in per-game metadata already in round_df; add prev-round columns via shift
    round_df_sorted = round_df.sort_values(["game_id", "round_number"])
    prev_cols = round_df_sorted.groupby("game_id")[
        ["overdrawn", "joint_efficiency", "joint_reward"]
    ].shift(1)
    prev_cols.columns = ["prev_overdrawn", "prev_joint_efficiency", "prev_joint_reward"]

    pairs = pairs.join(prev_cols, how="left")

    pairs["prev_joint_optimal"] = (
        ~pairs["prev_overdrawn"].fillna(True)
        & pairs["prev_joint_efficiency"].notna()
        & (pairs["prev_joint_efficiency"] >= 0.99)
    )

    return pairs[[
        "game_id", "experiment_label", "model_a", "model_b", "pair",
        "mode", "is_shifting", "mc_bucket", "share_projects", "think_about_opponent",
        "round_number",
        "prev_overdrawn", "prev_joint_efficiency", "prev_joint_reward", "prev_joint_optimal",
        "overdrawn", "joint_efficiency", "joint_reward",
        "alloc_same_as_prev", "joint_reward_improved", "stubborn_anchor",
    ]]



def _slice_summary(df: pd.DataFrame, label: str, groupby: str, total_pairs: int) -> None:
    """Print anchoring + improvement + stubborn-anchor rates grouped by a single column."""
    grp = df.groupby(groupby).agg(
        n=("game_id", "count"),
        pct_of_all_pairs=("game_id", lambda x: len(x) / total_pairs),
        pct_same_alloc=("alloc_same_as_prev", "mean"),
        pct_improved=("joint_reward_improved", "mean"),
        pct_prev_optimal=("prev_joint_optimal", "mean"),
        pct_stubborn_anchor=("stubborn_anchor", "mean"),
        mean_prev_eff=("prev_joint_efficiency", "mean"),
        mean_curr_eff=("joint_efficiency", "mean"),
    )
    print(f"\n{label}:")
    print(grp.round(3).to_string())


def print_summary(df: pd.DataFrame) -> None:
    print("=" * 60)
    print("RQ13: Strategy Anchoring & Persistence")
    print("=" * 60)

    if df.empty:
        print("No data.")
        return

    n_all = len(df)
    # Eligible = current round is suboptimal+non-overdrawn with a prior round and oracle data
    eligible = df["stubborn_anchor"].notna()
    n_eligible = eligible.sum()
    n_stubborn = df["stubborn_anchor"].sum()

    print(f"\nAll consecutive-round pairs (round > 1): {n_all}")
    print(f"  Of which current round overdrawn        : {df['overdrawn'].sum()} ({df['overdrawn'].mean():.1%})")
    print(f"  Of which current round already optimal  : {(df['joint_efficiency'] >= 0.99).sum()} ({(df['joint_efficiency'] >= 0.99).mean():.1%})")
    print(f"  Eligible (suboptimal+non-overdrawn)     : {n_eligible} ({n_eligible/n_all:.1%} of all pairs)")
    print(f"\nOf {n_eligible} eligible rounds:")
    print(f"  Stubborn anchor (same alloc as prev)    : {n_stubborn} ({df['stubborn_anchor'].mean():.1%})")
    print(f"  Changed allocation                      : {n_eligible - n_stubborn} ({1 - df['stubborn_anchor'].mean():.1%})")
    print(f"  Mean current efficiency                 : {df.loc[eligible, 'joint_efficiency'].mean():.3f}")
    print(f"  Mean current efficiency when anchored   : {df.loc[eligible & df['stubborn_anchor'], 'joint_efficiency'].mean():.3f}")
    print(f"  Mean current efficiency when changed    : {df.loc[eligible & ~df['stubborn_anchor'], 'joint_efficiency'].mean():.3f}")

    # --- Slices ---
    _slice_summary(df, "By prev round outcome (overdrawn)", "prev_overdrawn", n_all)
    _slice_summary(df, "By model pair", "pair", n_all)
    _slice_summary(df, "By mode", "mode", n_all)
    _slice_summary(df, "By mc_bucket", "mc_bucket", n_all)

    if df["share_projects"].notna().any():
        _slice_summary(df, "By share_projects", "share_projects", n_all)
    if df["think_about_opponent"].notna().any():
        _slice_summary(df, "By think_about_opponent", "think_about_opponent", n_all)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RQ13: Strategy Anchoring")
    add_common_args(parser)
    args = parser.parse_args()
    dataset = load_dataset_from_args(args)
    df = analyze_anchoring(dataset)
    print_summary(df)