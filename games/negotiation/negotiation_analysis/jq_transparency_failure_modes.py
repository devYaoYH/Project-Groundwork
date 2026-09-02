"""Compare failure mode rates between main cohort and full-transparency games.

Runs the four core failure-mode analyses (stable/shifting gap, stubborn anchoring,
perfunctory fairness, referential binding) on:
  - The Qwen×GPT-5 Mini slice of the main 720-game cohort (same pair, fair comparison)
  - The 120-game full-transparency subset (same pair, all conditions)

Usage:
    uv run python -m scripts.analysis.jq_transparency_failure_modes
"""

import pandas as pd

from negotiation_analysis.data_loader import (
    load_dataset,
    MAIN_COHORT_RUN_IDS,
    TOMBSTONED_GAME_IDS,
)
from negotiation_analysis.models import NegotiationDataset
from negotiation_analysis.rq13_anchoring import analyze_anchoring
from negotiation_analysis.rq16_perfunctory_fairness import analyze_perfunctory_fairness
from negotiation_analysis.rq20_referential_binding_heuristic import analyze_binding_failures
from negotiation_analysis.rq3_overdraw_prevalence import analyze_overdraw_prevalence

TRANSPARENCY_RUN_IDS = frozenset({
    "56abe7a8-2d59-4b7b-9d4d-80cbdd078a65",
    "ac21edea-41ae-4953-894c-0327927b0e8a",
})

# Cross-play run ID for Qwen×GPT-5 Mini in the main cohort
BASELINE_PAIR_RUN_ID = "6cb004cb-1097-4c87-9003-679a41343733"


def _filter_dataset(ds: NegotiationDataset, run_ids: frozenset, pair_substring: str = "") -> NegotiationDataset:
    games = [
        g for g in ds.games
        if g.config.get("experiment_run_id") in run_ids
        and g.game_id not in TOMBSTONED_GAME_IDS
        and (pair_substring == "" or pair_substring in g.label)
    ]
    return NegotiationDataset(games)


def _run_all(ds: NegotiationDataset, label: str) -> dict:
    round_df = ds.to_round_df()
    n_games = len(ds.games)
    n_rounds = len(round_df)

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"  {n_games} games, {n_rounds} rounds")
    print(f"{'='*60}")

    # --- Core outcome metrics ---
    overdraw_pct = round(round_df["overdrawn"].mean() * 100, 1)
    efficiency_pct = round(round_df["joint_efficiency"].mean() * 100, 1)
    is_optimal = round_df["joint_efficiency"] >= 0.95
    optimum_pct = round(is_optimal.mean() * 100, 1)
    print(f"  Overdraw:   {overdraw_pct}%")
    print(f"  Efficiency: {efficiency_pct}%")
    print(f"  Optimum:    {optimum_pct}%")

    # --- Stable/shifting gap ---
    by_mode = round_df.groupby("mode")["joint_efficiency"].agg(
        optimum_rate=lambda x: (x >= 0.95).mean() * 100
    )
    stable_opt = round(by_mode.loc["stable", "optimum_rate"], 1) if "stable" in by_mode.index else float("nan")
    shifting_opt = round(by_mode.loc["shifting", "optimum_rate"], 1) if "shifting" in by_mode.index else float("nan")
    gap_pp = round(stable_opt - shifting_opt, 1)
    print(f"\n  [Stable/Shifting gap]")
    print(f"  Stable optimum:   {stable_opt}%")
    print(f"  Shifting optimum: {shifting_opt}%")
    print(f"  Gap:              {gap_pp:+.1f} pp")

    # --- Stubborn anchoring ---
    anchor_df = analyze_anchoring(ds)
    # stubborn_anchor is True when: alloc same as prev, round was suboptimal but not overdrawn
    anchor_eligible = anchor_df[
        anchor_df["alloc_same_as_prev"].notna()
        & ~anchor_df["prev_overdrawn"].fillna(True)
        & (anchor_df["joint_efficiency"] < 0.95)
        & ~anchor_df["overdrawn"]
    ]
    stubborn_rate = round(anchor_df["stubborn_anchor"].dropna().mean() * 100, 1)
    print(f"\n  [Stubborn anchoring]")
    print(f"  Stubborn anchor rate (of repeat-eligible rounds): {stubborn_rate}%")

    # --- Perfunctory fairness ---
    fairness_result = analyze_perfunctory_fairness(ds)
    fair_summary = fairness_result["summary"]
    equal_split_pct = round(fair_summary["equal_split_rate"] * 100, 1)
    perf_fair_pct = round(fair_summary["perfunctory_fair_rate"] * 100, 1)
    print(f"\n  [Perfunctory fairness]")
    print(f"  Equal split rate:          {equal_split_pct}%")
    print(f"  Perfunctory fairness rate: {perf_fair_pct}%")

    # --- Referential binding ---
    binding_df = analyze_binding_failures(ds)
    total_overdrawn = int(round_df["overdrawn"].sum())
    agreement_overdraw = len(binding_df)
    binding_rate = round(agreement_overdraw / total_overdrawn * 100, 1) if total_overdrawn > 0 else float("nan")
    print(f"\n  [Referential binding failures]")
    print(f"  Overdrawn rounds with prior agreement language: {agreement_overdraw}/{total_overdrawn} ({binding_rate}%)")

    return {
        "label": label,
        "n_games": n_games,
        "n_rounds": n_rounds,
        "overdraw_pct": overdraw_pct,
        "efficiency_pct": efficiency_pct,
        "optimum_pct": optimum_pct,
        "stable_optimum_pct": stable_opt,
        "shifting_optimum_pct": shifting_opt,
        "gap_pp": gap_pp,
        "stubborn_anchor_pct": stubborn_rate,
        "equal_split_pct": equal_split_pct,
        "perfunctory_fairness_pct": perf_fair_pct,
        "agreement_then_overdraw_pct": binding_rate,
    }


def main():
    full_ds = load_dataset()

    # Main cohort: Qwen×GPT-5 Mini cross-play slice (apples-to-apples comparison)
    baseline_ds = _filter_dataset(full_ds, {BASELINE_PAIR_RUN_ID}, pair_substring="gpt5m-qwen")
    transp_ds = _filter_dataset(full_ds, TRANSPARENCY_RUN_IDS)

    b = _run_all(baseline_ds, "Baseline: Qwen×GPT-5 Mini (main cohort, N=120)")
    t = _run_all(transp_ds,   "Full-transparency: Qwen×GPT-5 Mini (N=120)")

    print(f"\n{'='*60}")
    print("  COMPARISON SUMMARY")
    print(f"{'='*60}")
    metrics = [
        ("overdraw_pct",               "Overdraw (%)"),
        ("efficiency_pct",             "Efficiency (%)"),
        ("optimum_pct",                "Optimum (%)"),
        ("stable_optimum_pct",         "Optimum stable (%)"),
        ("shifting_optimum_pct",       "Optimum shifting (%)"),
        ("gap_pp",                     "Stable/shifting gap (pp)"),
        ("stubborn_anchor_pct",        "Stubborn anchor (%)"),
        ("equal_split_pct",            "Equal split (%)"),
        ("perfunctory_fairness_pct",   "Perfunctory fairness (%)"),
        ("agreement_then_overdraw_pct","Agreement-then-overdraw (%)"),
    ]
    print(f"  {'Metric':<38} {'Baseline':>10} {'Transparent':>12} {'Delta':>8}")
    print(f"  {'-'*72}")
    for key, metric_label in metrics:
        bv = b.get(key, float("nan"))
        tv = t.get(key, float("nan"))
        try:
            delta = round(tv - bv, 1)
            delta_str = f"{delta:+.1f}"
        except TypeError:
            delta_str = "n/a"
        print(f"  {metric_label:<38} {str(bv):>10} {str(tv):>12} {delta_str:>8}")


if __name__ == "__main__":
    main()
