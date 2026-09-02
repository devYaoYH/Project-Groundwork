"""Process Metrics — Consolidated computation for the paper's process metrics table.

Metrics:
    1. Stated-vs-actual coherence: Jaccard overlap between stated intent and actual allocation
    2. Strategy taxonomy: utterance classification (STUB — requires LLM-as-judge)
    3. First-proposal deference: degree to which responder matches first proposal
    4. Allocation anchoring: consecutive-round allocation similarity
    5. Referential binding: grounding quality classification (STUB — requires LLM-as-judge)
    6. Project mention rate: fraction of utterances referencing project names
    7. Information density: ratio of content words to total words per utterance

Usage:
    uv run python -m scripts.analysis.process_metrics
    uv run python -m scripts.analysis.process_metrics --run-id a1b2c3d4
    uv run python -m scripts.analysis.process_metrics --min-schema 5 --model sonnet gpt5m
"""

import argparse

import numpy as np
import pandas as pd

from negotiation_analysis.data_loader import (
    add_common_args,
    load_dataset_from_args,
)
from negotiation_analysis.models import NegotiationDataset
from negotiation_analysis.rq6_stated_vs_actual import analyze_stated_vs_actual
from negotiation_analysis.rq12_first_proposal_deference import analyze_first_proposal_deference
from negotiation_analysis.rq13_anchoring import analyze_anchoring
from negotiation_analysis.rq15_information_density import analyze_information_density
from negotiation_analysis.rq18_strategy_taxonomy import analyze_strategy_taxonomy


def compute_process_metrics(dataset: NegotiationDataset) -> dict:
    """Compute all process metrics for V5+ project-based games.

    Returns dict with:
        - game_df: one row per game with process metrics
        - stated_vs_actual: detailed stated-vs-actual results
        - anchoring: detailed anchoring results
        - first_proposal: detailed first-proposal deference results
        - summary: aggregate statistics
    """
    round_df = dataset.to_round_df()
    raw_games = [g.raw for g in dataset.games]

    if round_df.empty:
        return {"game_df": pd.DataFrame(), "summary": {}}

    # 1. Stated-vs-actual coherence (takes raw_games)
    sva_results = analyze_stated_vs_actual(raw_games)
    sva_df = sva_results.get("cohere_df", pd.DataFrame())
    if not sva_df.empty and "resource_match" in sva_df.columns:
        sva_by_game = (
            sva_df.groupby("game_id")["resource_match"]
            .mean()
            .rename("stated_actual_coherence")
        )
    else:
        sva_by_game = pd.Series(dtype=float, name="stated_actual_coherence")

    # 3. First-proposal deference (takes NegotiationDataset)
    fpd_results = analyze_first_proposal_deference(dataset)
    fpd_df = fpd_results.get("round_df", pd.DataFrame())
    if not fpd_df.empty and "opponent_deference_resource_match" in fpd_df.columns:
        fpd_by_game = (
            fpd_df.groupby("game_id")["opponent_deference_resource_match"]
            .mean()
            .rename("first_proposal_deference")
        )
    else:
        fpd_by_game = pd.Series(dtype=float, name="first_proposal_deference")

    # 4. Allocation anchoring (returns DataFrame directly)
    anch_df = analyze_anchoring(dataset)
    if not anch_df.empty and "stubborn_anchor" in anch_df.columns:
        anch_by_game = anch_df.groupby("game_id").agg(
            stubborn_anchor_rate=pd.NamedAgg(column="stubborn_anchor", aggfunc="mean"),
        )
    else:
        anch_by_game = pd.DataFrame(columns=["stubborn_anchor_rate"])

    # Build game-level DataFrame
    game_df = (
        round_df.groupby("game_id")[["model_a", "model_b", "pair", "is_cross_play", "mode", "mc_bucket", "is_rotating"]]
        .first()
        .reset_index()
    )

    # Merge metrics
    game_df = game_df.merge(sva_by_game, on="game_id", how="left")
    game_df = game_df.merge(fpd_by_game, on="game_id", how="left")
    game_df = game_df.merge(anch_by_game, on="game_id", how="left")

    # Project mention frequency (from rq6)
    proj_df = sva_results.get("project_df", pd.DataFrame())
    if not proj_df.empty and "project_mention_rate" in proj_df.columns:
        proj_by_game = (
            proj_df[proj_df["num_messages"] > 0]
            .groupby("game_id")["project_mention_rate"]
            .mean()
            .rename("project_mention_rate")
        )
        game_df = game_df.merge(proj_by_game, on="game_id", how="left")
    else:
        game_df["project_mention_rate"] = np.nan

    # Information density (per-game mean across turns)
    id_results = analyze_information_density(raw_games)
    id_turn_df = id_results.get("turn_df", pd.DataFrame())
    if not id_turn_df.empty and "density" in id_turn_df.columns:
        id_by_game = (
            id_turn_df.groupby("game_id")["density"]
            .mean()
            .rename("information_density")
        )
        game_df = game_df.merge(id_by_game, on="game_id", how="left")
    else:
        game_df["information_density"] = np.nan

    # 2. Strategy taxonomy (sharing, proposal, fairness, threat, turn-taking, WSLS)
    tax_results = analyze_strategy_taxonomy(dataset)
    tax_game_df = tax_results.get("game_df", pd.DataFrame())
    tax_cols = [
        "sharing_rate", "proposal_rate", "fairness_appeal_rate", "threat_rate",
        "turn_taking_rate_2", "turn_taking_rate_4", "win_stay_rate", "lose_shift_rate",
    ]
    if not tax_game_df.empty:
        merge_cols = ["game_id"] + [c for c in tax_cols if c in tax_game_df.columns]
        game_df = game_df.merge(tax_game_df[merge_cols], on="game_id", how="left")

    # 5. Referential binding — STUB
    game_df["referential_binding"] = None  # Deferred: requires LLM-as-judge

    summary = {
        "n_games": len(game_df),
        "mean_stated_actual_coherence": game_df["stated_actual_coherence"].mean(),
        "mean_first_proposal_deference": game_df["first_proposal_deference"].mean(),
        "mean_stubborn_anchor_rate": game_df["stubborn_anchor_rate"].mean()
        if "stubborn_anchor_rate" in game_df.columns else np.nan,
        "mean_project_mention_rate": game_df["project_mention_rate"].mean(),
        "mean_information_density": game_df["information_density"].mean(),
        "mean_sharing_rate": game_df["sharing_rate"].mean() if "sharing_rate" in game_df.columns else np.nan,
        "mean_proposal_rate": game_df["proposal_rate"].mean() if "proposal_rate" in game_df.columns else np.nan,
        "mean_fairness_appeal_rate": game_df["fairness_appeal_rate"].mean() if "fairness_appeal_rate" in game_df.columns else np.nan,
        "mean_threat_rate": game_df["threat_rate"].mean() if "threat_rate" in game_df.columns else np.nan,
        "mean_turn_taking_2": game_df["turn_taking_rate_2"].mean() if "turn_taking_rate_2" in game_df.columns else np.nan,
        "mean_win_stay_rate": game_df["win_stay_rate"].mean() if "win_stay_rate" in game_df.columns else np.nan,
        "mean_lose_shift_rate": game_df["lose_shift_rate"].mean() if "lose_shift_rate" in game_df.columns else np.nan,
    }

    return {
        "game_df": game_df,
        "stated_vs_actual": sva_results,
        "anchoring_df": anch_df,
        "first_proposal": fpd_results,
        "information_density": id_results,
        "strategy_taxonomy": tax_results,
        "summary": summary,
    }


def print_summary(results: dict) -> None:
    print("=" * 70)
    print("PROCESS METRICS (V5+ Project-Based Games)")
    print("=" * 70)

    s = results["summary"]
    print(f"\nGames analyzed: {s['n_games']}")
    print(f"\n{'Metric':<35} {'Value':>10}")
    print("-" * 47)
    if not np.isnan(s.get("mean_stated_actual_coherence", np.nan)):
        print(f"{'Stated-vs-actual coherence':<35} {s['mean_stated_actual_coherence']:>10.3f}")
    if not np.isnan(s.get("mean_first_proposal_deference", np.nan)):
        print(f"{'First-proposal deference':<35} {s['mean_first_proposal_deference']:>10.3f}")
    if not np.isnan(s.get("mean_stubborn_anchor_rate", np.nan)):
        print(f"{'Stubborn anchor rate':<35} {s['mean_stubborn_anchor_rate']:>10.3f}")
    if not np.isnan(s.get("mean_project_mention_rate", np.nan)):
        print(f"{'Project mention rate':<35} {s['mean_project_mention_rate']:>10.3f}")
    if not np.isnan(s.get("mean_information_density", np.nan)):
        print(f"{'Information density':<35} {s['mean_information_density']:>10.3f}")
    if not np.isnan(s.get("mean_sharing_rate", np.nan)):
        print(f"{'Sharing (self-proposal) rate':<35} {s['mean_sharing_rate']:>10.3f}")
    if not np.isnan(s.get("mean_proposal_rate", np.nan)):
        print(f"{'Proposal (other-proposal) rate':<35} {s['mean_proposal_rate']:>10.3f}")
    if not np.isnan(s.get("mean_fairness_appeal_rate", np.nan)):
        print(f"{'Fairness appeal rate':<35} {s['mean_fairness_appeal_rate']:>10.3f}")
    if not np.isnan(s.get("mean_threat_rate", np.nan)):
        print(f"{'Threat rate':<35} {s['mean_threat_rate']:>10.3f}")
    if not np.isnan(s.get("mean_turn_taking_2", np.nan)):
        print(f"{'Turn-taking (2-round)':<35} {s['mean_turn_taking_2']:>10.3f}")
    if not np.isnan(s.get("mean_win_stay_rate", np.nan)):
        print(f"{'Win-stay rate':<35} {s['mean_win_stay_rate']:>10.3f}")
    if not np.isnan(s.get("mean_lose_shift_rate", np.nan)):
        print(f"{'Lose-shift rate':<35} {s['mean_lose_shift_rate']:>10.3f}")
    print(f"{'Referential binding':<35} {'STUB':>10}")

    # By pair
    gdf = results["game_df"]
    if "pair" in gdf.columns and gdf["pair"].nunique() > 1:
        print("\nBy pair:")
        cols = [
            "stated_actual_coherence", "first_proposal_deference", "stubborn_anchor_rate",
            "information_density", "sharing_rate", "proposal_rate",
            "fairness_appeal_rate", "threat_rate", "turn_taking_rate_2",
            "win_stay_rate", "lose_shift_rate",
        ]
        existing = [c for c in cols if c in gdf.columns]
        if existing:
            by_pair = gdf.groupby("pair")[existing].mean().round(3)
            print(by_pair.to_string())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compute process metrics for V5+ games")
    add_common_args(parser)
    args = parser.parse_args()
    dataset = load_dataset_from_args(args)
    print(f"Loaded {len(dataset.games)} games")
    results = compute_process_metrics(dataset)
    print_summary(results)
