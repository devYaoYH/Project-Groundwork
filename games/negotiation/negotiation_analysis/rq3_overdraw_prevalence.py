"""RQ3: Overdraw Prevalence by Condition.

Are overdraws more prevalent in collaborative shifting and competitive conditions?

Usage:
    uv run python scripts/analysis/rq3_overdraw_prevalence.py
"""

import pandas as pd
import argparse
from scipy import stats

from negotiation_analysis.data_loader import add_common_args, load_dataset_from_args, RESOURCE_SUPPLY
from negotiation_analysis.models import NegotiationDataset


def analyze_overdraw_prevalence(dataset: NegotiationDataset) -> dict:
    """Compute overdraw rates across conditions."""
    round_df = dataset.to_round_df()

    # Per-environment overdraw rate
    game_od = (
        round_df.groupby(["episode_uid", "mode"])
        .agg(
            n_rounds=pd.NamedAgg(column="round_number", aggfunc="count"),
            n_overdrawn=pd.NamedAgg(column="overdrawn", aggfunc="sum"),
        )
        .reset_index()
    )
    game_od["overdraw_rate"] = game_od["n_overdrawn"] / game_od["n_rounds"]

    # Summary by mode
    od_summary = (
        game_od.groupby("mode")["overdraw_rate"]
        .describe()
        .round(3)
    )

    # Overdraw rate by round × mode (multi-round games only)
    multi_round_ids = set(
        round_df[round_df["num_game_rounds"] >= 2]["episode_uid"]
    )
    mr_rounds = round_df[round_df["episode_uid"].isin(multi_round_ids)]
    od_by_round_cond = (
        mr_rounds.groupby(["mode", "round_number"])
        .agg(
            overdraw_rate=pd.NamedAgg(column="overdrawn", aggfunc="mean"),
            n=pd.NamedAgg(column="episode_uid", aggfunc="count"),
        )
        .reset_index()
    )

    # Which resources get overdrawn?
    od_rounds = round_df[round_df["overdrawn"] == True]
    overdrawn_resources = []
    for _, row in od_rounds.iterrows():
        joint = row.get("joint_resources") or {}
        if isinstance(joint, dict):
            for res, qty in joint.items():
                supply_limit = RESOURCE_SUPPLY.get(res, 10)
                if qty > supply_limit:
                    overdrawn_resources.append({"resource": res, "mode": row["mode"]})
    od_resource_df = pd.DataFrame(overdrawn_resources) if overdrawn_resources else pd.DataFrame()

    # Stable vs shifting
    stable = game_od[game_od["mode"] == "stable"]["overdraw_rate"]
    shifting = game_od[game_od["mode"] == "shifting"]["overdraw_rate"]
    stable_vs_shifting = None
    if len(stable) > 1 and len(shifting) > 1:
        u, p = stats.mannwhitneyu(stable, shifting, alternative="two-sided")
        stable_vs_shifting = {
            "U": u, "p": p,
            "stable_mean": stable.mean(), "shifting_mean": shifting.mean(),
        }

    return {
        "game_od": game_od,
        "od_summary": od_summary,
        "od_by_round_cond": od_by_round_cond,
        "od_resource_df": od_resource_df,
        "stable_vs_shifting": stable_vs_shifting,
    }


def print_summary(results: dict) -> None:
    print("=" * 60)
    print("RQ3: Overdraw Prevalence by Condition")
    print("=" * 60)

    print("\nOverdraw rate by mode:")
    print(results["od_summary"])

    if results["stable_vs_shifting"]:
        t = results["stable_vs_shifting"]
        print(f"\nStable vs Shifting:")
        print(f"  Stable: {t['stable_mean']:.3f}")
        print(f"  Shifting: {t['shifting_mean']:.3f}")
        print(f"  Mann-Whitney p={t['p']:.4f}")

    if not results["od_resource_df"].empty:
        print("\nOverdrawn resources:")
        counts = results["od_resource_df"].groupby(
            ["mode", "resource"]
        ).size()
        print(counts.to_string())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RQ3: Overdraw Prevalence")
    add_common_args(parser)
    args = parser.parse_args()
    dataset = load_dataset_from_args(args)
    results = analyze_overdraw_prevalence(dataset)
    print_summary(results)