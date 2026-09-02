"""RQ2: Common Ground Repair After Overdraw (Stable Condition).

When overdraw occurs in the stable condition, is there common ground repair?
Examines speech volume changes, allocation adjustments, and recovery patterns.

Usage:
    uv run python scripts/analysis/rq2_common_ground_repair.py
"""

import numpy as np
import argparse
import pandas as pd
from scipy import stats

from negotiation_analysis.data_loader import add_common_args, load_dataset_from_args
from negotiation_analysis.models import NegotiationDataset


def _compute_alloc_change_l1(grp: pd.DataFrame, alloc_cols: list[str]) -> pd.Series:
    """Compute L1 distance of allocation changes from previous round."""
    vals = grp[alloc_cols].values.astype(float)
    changes = np.concatenate(
        [[np.nan], np.abs(np.diff(vals, axis=0)).sum(axis=1)]
    )
    return pd.Series(changes, index=grp.index)


def analyze(dataset: NegotiationDataset) -> dict:
    """Analyze repair patterns after overdraw in stable condition."""
    round_df = dataset.to_round_df()

    multi_round_ids = set(
        round_df[round_df["num_game_rounds"] >= 2]["game_id"]
    )
    stable_rounds = (
        round_df[
            (round_df["mode"] == "stable")
            & (round_df["game_id"].isin(multi_round_ids))
        ]
        .sort_values(["game_id", "round_number"])
        .copy()
    )

    # Previous round overdraw flag
    stable_rounds["prev_overdrawn"] = (
        stable_rounds.groupby("game_id")["overdrawn"].shift(1).fillna(False)
    )

    # Extract per-resource allocation columns for L1 distance computation
    for prefix, col in [("agent_a", "agent_a_resources"), ("agent_b", "agent_b_resources")]:
        for res in ["wood", "stone", "gold"]:
            stable_rounds[f"{prefix}_{res}"] = stable_rounds[col].apply(
                lambda d: d.get(res, 0) if isinstance(d, dict) else 0
            )

    alloc_cols = [
        "agent_a_wood", "agent_a_stone", "agent_a_gold",
        "agent_b_wood", "agent_b_stone", "agent_b_gold",
    ]

    stable_rounds["alloc_change_l1"] = stable_rounds.groupby(
        "game_id", group_keys=False
    ).apply(lambda grp: _compute_alloc_change_l1(grp, alloc_cols))

    post_r1 = stable_rounds[stable_rounds["round_number"] > 1].copy()

    # Speech volume comparison
    after_od = post_r1[post_r1["prev_overdrawn"] == True]["total_speech_chars"]
    normal = post_r1[post_r1["prev_overdrawn"] == False]["total_speech_chars"]
    speech_test = None
    if len(after_od) > 0 and len(normal) > 0:
        u, p = stats.mannwhitneyu(after_od, normal, alternative="two-sided")
        speech_test = {
            "U": u, "p": p,
            "after_od_mean": after_od.mean(),
            "normal_mean": normal.mean(),
            "after_od_n": len(after_od),
            "normal_n": len(normal),
        }

    # Allocation change comparison
    od_change = post_r1[post_r1["prev_overdrawn"] == True]["alloc_change_l1"].dropna()
    no_change = post_r1[post_r1["prev_overdrawn"] == False]["alloc_change_l1"].dropna()
    alloc_test = None
    if len(od_change) > 0 and len(no_change) > 0:
        u, p = stats.mannwhitneyu(od_change, no_change, alternative="two-sided")
        alloc_test = {
            "U": u, "p": p,
            "after_od_mean": od_change.mean(),
            "normal_mean": no_change.mean(),
        }

    # Recovery rate
    post_r1["recovered"] = ~post_r1["overdrawn"]
    recovery_data = post_r1[post_r1["prev_overdrawn"] == True]
    recovery_rate = recovery_data["recovered"].mean() if len(recovery_data) > 0 else None

    # Repair latency
    repair_records = []
    for gid, grp in stable_rounds.groupby("game_id"):
        grp = grp.sort_values("round_number").reset_index(drop=True)
        od_list = grp["overdrawn"].tolist()
        i = 0
        while i < len(od_list):
            if od_list[i]:
                j = i + 1
                while j < len(od_list) and od_list[j]:
                    j += 1
                repair_records.append({
                    "game_id": gid,
                    "overdraw_round": grp.loc[i, "round_number"],
                    "latency": j - i,
                    "recovered": j < len(od_list),
                })
                i = j
            else:
                i += 1

    repair_df = pd.DataFrame(repair_records) if repair_records else pd.DataFrame()

    return {
        "stable_rounds": stable_rounds,
        "post_r1": post_r1,
        "speech_test": speech_test,
        "alloc_test": alloc_test,
        "recovery_rate": recovery_rate,
        "recovery_n": len(recovery_data),
        "repair_df": repair_df,
    }


def print_summary(results: dict) -> None:
    """Print a text summary of RQ2 results."""
    print("=" * 60)
    print("RQ2: Common Ground Repair After Overdraw (Stable)")
    print("=" * 60)

    if results["speech_test"]:
        t = results["speech_test"]
        print(f"\nSpeech volume after overdraw vs normal:")
        print(f"  After OD: {t['after_od_mean']:.0f} chars (n={t['after_od_n']})")
        print(f"  Normal:   {t['normal_mean']:.0f} chars (n={t['normal_n']})")
        print(f"  Mann-Whitney p={t['p']:.4f}")

    if results["alloc_test"]:
        t = results["alloc_test"]
        print(f"\nAllocation change (L1) after overdraw vs normal:")
        print(f"  After OD: {t['after_od_mean']:.2f}")
        print(f"  Normal:   {t['normal_mean']:.2f}")
        print(f"  Mann-Whitney p={t['p']:.4f}")

    if results["recovery_rate"] is not None:
        print(f"\nRecovery rate: {results['recovery_rate']:.1%} (n={results['recovery_n']})")

    if not results["repair_df"].empty:
        df = results["repair_df"]
        print(f"\nRepair latency: mean={df['latency'].mean():.2f} rounds")
        print(f"  Distribution: {df['latency'].value_counts().sort_index().to_dict()}")
        print(f"  Overall recovery: {df['recovered'].mean():.1%}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RQ2: Common Ground Repair")
    add_common_args(parser)
    args = parser.parse_args()
    dataset = load_dataset_from_args(args)
    results = analyze(dataset)
    print_summary(results)
