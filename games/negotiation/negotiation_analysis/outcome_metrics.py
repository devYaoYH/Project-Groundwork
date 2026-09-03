"""Outcome Metrics — Consolidated computation for the paper's outcome metrics table.

Metrics:
    1. Allocation efficiency: joint_reward / oracle_collab_max
    2. Overdraw rate: fraction of rounds where demand exceeds supply
    3. Value sharing rate: fraction of utterances disclosing private project info
    4. Common ground repair: recovery rate and latency after overdraw
    5. Optimum convergence: round at which efficiency first exceeds 90%
    6. Speech compression: token reduction per round over time

Usage:
    uv run python -m scripts.analysis.outcome_metrics
    uv run python -m scripts.analysis.outcome_metrics --run-id a1b2c3d4
    uv run python -m scripts.analysis.outcome_metrics --min-schema 5 --model sonnet gpt5m
"""

import argparse

import numpy as np
import pandas as pd
from scipy import stats

from negotiation_analysis.data_loader import add_common_args, load_dataset_from_args
from negotiation_analysis.models import NegotiationDataset
from negotiation_analysis.rq4_value_sharing import analyze_value_sharing
from negotiation_analysis.rq16_perfunctory_fairness import analyze_perfunctory_fairness


def compute_outcome_metrics(dataset: NegotiationDataset) -> dict:
    """Compute all outcome metrics for V5+ project-based games.

    Returns dict with:
        - game_df: one row per environment with all outcome metrics
        - round_df: per-round efficiency and overdraw data
        - summary: aggregate statistics
    """
    round_df = dataset.to_round_df()

    if round_df.empty:
        return {"game_df": pd.DataFrame(), "round_df": pd.DataFrame(), "summary": {}}

    game_records = []
    for g in dataset.games:
        gid = g.episode_uid
        gr = round_df[round_df["episode_uid"] == gid]
        if len(gr) == 0:
            continue
        pair = gr["pair"].iloc[0]
        model_a = gr["model_a"].iloc[0]
        model_b = gr["model_b"].iloc[0]
        is_cross_play = gr["is_cross_play"].iloc[0]

        # 1. Allocation efficiency (mean across rounds)
        efficiencies = gr["joint_efficiency"].dropna()
        mean_efficiency = efficiencies.mean() if len(efficiencies) > 0 else np.nan

        # 2. Overdraw rate
        overdraw_rate = gr["overdrawn"].mean()

        # 5. Optimum convergence: first round where efficiency == 100%
        above = gr[gr["joint_efficiency"] >= 1.0]["round_number"]
        convergence_round = above.iloc[0] if len(above) > 0 else np.nan

        # 6. Speech compression: Spearman(total_speech_chars, round_number)
        speech_chars = gr["total_speech_chars"]
        round_nums = gr["round_number"]
        if len(speech_chars) >= 3 and speech_chars.std() > 0:
            spearman_r, spearman_p = stats.spearmanr(round_nums, speech_chars)
        else:
            spearman_r, spearman_p = np.nan, np.nan

        # 4. Common ground repair
        sorted_rounds = gr.sort_values("round_number")
        prev_od = sorted_rounds["overdrawn"].shift(1).fillna(False)
        post_od = sorted_rounds[prev_od]
        recovery_rate = (~post_od["overdrawn"]).mean() if len(post_od) > 0 else np.nan

        # Repair latency
        od_list = sorted_rounds["overdrawn"].tolist()
        latencies = []
        i = 0
        while i < len(od_list):
            if od_list[i]:
                j = i + 1
                while j < len(od_list) and od_list[j]:
                    j += 1
                latencies.append(j - i)
                i = j
            else:
                i += 1
        mean_repair_latency = np.mean(latencies) if latencies else np.nan

        # Individual fair efficiencies (mean across rounds)
        a_fair_effs = gr["agent_a_fair_efficiency"].dropna()
        b_fair_effs = gr["agent_b_fair_efficiency"].dropna()

        game_records.append({
            "episode_uid": gid,
            "model_a": model_a,
            "model_b": model_b,
            "pair": pair,
            "is_cross_play": is_cross_play,
            "mode": g.mode,
            "mc_bucket": gr["mc_bucket"].iloc[0],
            "is_rotating": gr["is_rotating"].iloc[0],
            "experiment_label": g.label,
            "num_rounds": g.num_rounds,
            # Joint outcome metrics
            "allocation_efficiency": mean_efficiency,
            "overdraw_rate": overdraw_rate,
            "convergence_round": convergence_round,
            "speech_compression_r": spearman_r,
            "speech_compression_p": spearman_p,
            "recovery_rate": recovery_rate,
            "mean_repair_latency": mean_repair_latency,
            # Individual fair efficiency (vs collab_max/2)
            "agent_a_fair_eff": a_fair_effs.mean() if len(a_fair_effs) > 0 else np.nan,
            "agent_b_fair_eff": b_fair_effs.mean() if len(b_fair_effs) > 0 else np.nan,
        })

    game_df = pd.DataFrame(game_records)

    # 3. Value sharing rate (per-environment, uses turn_df)
    vs_results = analyze_value_sharing(dataset)
    vs_turn_df = vs_results.get("turn_df", pd.DataFrame())
    if not vs_turn_df.empty and "shares_values" in vs_turn_df.columns:
        vs_by_game = (
            vs_turn_df.groupby("episode_uid")["shares_values"]
            .mean()
            .rename("value_sharing_rate")
        )
        game_df = game_df.merge(vs_by_game, on="episode_uid", how="left")
    else:
        game_df["value_sharing_rate"] = np.nan

    # 7. Perfunctory fairness (equal-split failure mode)
    pf_results = analyze_perfunctory_fairness(dataset)
    pf_rdf = pf_results.get("round_df", pd.DataFrame())
    if not pf_rdf.empty:
        pf_by_game = pf_rdf.groupby("episode_uid").agg(
            equal_split_rate=pd.NamedAgg(column="has_equal_split", aggfunc="mean"),
            perfunctory_fair_rate=pd.NamedAgg(column="perfunctory_fair", aggfunc="mean"),
        )
        game_df = game_df.merge(pf_by_game, on="episode_uid", how="left")
    else:
        game_df["equal_split_rate"] = np.nan
        game_df["perfunctory_fair_rate"] = np.nan

    # Summary statistics
    summary = {
        "n_games": len(game_df),
        "mean_efficiency": game_df["allocation_efficiency"].mean(),
        "mean_overdraw_rate": game_df["overdraw_rate"].mean(),
        "mean_value_sharing_rate": game_df["value_sharing_rate"].mean(),
        "mean_convergence_round": game_df["convergence_round"].mean(),
        "pct_converged": game_df["convergence_round"].notna().mean(),
        "mean_speech_compression_r": game_df["speech_compression_r"].mean(),
        "mean_recovery_rate": game_df["recovery_rate"].mean(),
        "mean_equal_split_rate": game_df["equal_split_rate"].mean(),
        "mean_perfunctory_fair_rate": game_df["perfunctory_fair_rate"].mean(),
    }

    # Build per-model individual efficiency (melt: each environment → 2 rows)
    model_records = []
    for _, row in game_df.iterrows():
        model_records.append({
            "model": row["model_a"], "fair_eff": row["agent_a_fair_eff"],
            "role": "agent_a", "episode_uid": row["episode_uid"],
            "opponent": row["model_b"], "is_cross_play": row["is_cross_play"],
        })
        model_records.append({
            "model": row["model_b"], "fair_eff": row["agent_b_fair_eff"],
            "role": "agent_b", "episode_uid": row["episode_uid"],
            "opponent": row["model_a"], "is_cross_play": row["is_cross_play"],
        })
    model_df = pd.DataFrame(model_records)

    return {
        "game_df": game_df,
        "round_df": round_df,
        "model_df": model_df,
        "summary": summary,
    }


def build_model_round_df(dataset: NegotiationDataset, optimality_threshold: float = 0.9) -> pd.DataFrame:
    """Build a per-round DataFrame with efficiency and optimality, sliceable by condition."""
    round_df = dataset.to_round_df()
    if round_df.empty:
        return pd.DataFrame()

    cols = [
        "episode_uid", "round_number", "model_a", "model_b", "pair", "is_cross_play",
        "mode", "is_shifting", "is_rotating", "mc_bucket", "experiment_label",
        "joint_efficiency",
        "agent_a_fair_efficiency", "agent_b_fair_efficiency",
    ]
    df = round_df[[c for c in cols if c in round_df.columns]].copy()
    df["is_optimal"] = df["joint_efficiency"] >= optimality_threshold
    return df


def print_summary(results: dict) -> None:
    print("=" * 70)
    print("OUTCOME METRICS (V5+ Project-Based Games)")
    print("=" * 70)

    s = results["summary"]
    print(f"\nGames analyzed: {s['n_games']}")
    print(f"\n{'Metric':<30} {'Value':>10}")
    print("-" * 42)
    print(f"{'Allocation efficiency':<30} {s['mean_efficiency']:>10.3f}")
    print(f"{'Overdraw rate':<30} {s['mean_overdraw_rate']:>10.3f}")
    print(f"{'Value sharing rate':<30} {s['mean_value_sharing_rate']:>10.3f}")
    print(f"{'Recovery rate':<30} {s['mean_recovery_rate']:>10.3f}")
    print(f"{'Converged to 100% (%)':<30} {s['pct_converged']:>10.1%}")
    print(f"{'Mean convergence round':<30} {s['mean_convergence_round']:>10.1f}")
    print(f"{'Speech compression (rho)':<30} {s['mean_speech_compression_r']:>10.3f}")
    print(f"{'Equal-split rate':<30} {s['mean_equal_split_rate']:>10.3f}")
    print(f"{'Perfunctory fair rate':<30} {s['mean_perfunctory_fair_rate']:>10.3f}")

    # By pair (supports self-play and cross-play)
    gdf = results["game_df"]
    if "pair" in gdf.columns and gdf["pair"].nunique() > 1:
        print("\nBy pair:")
        by_pair = gdf.groupby("pair").agg(
            n=pd.NamedAgg(column="episode_uid", aggfunc="count"),
            joint_eff=pd.NamedAgg(column="allocation_efficiency", aggfunc="mean"),
            overdraw=pd.NamedAgg(column="overdraw_rate", aggfunc="mean"),
        ).round(3)
        print(by_pair.to_string())

    # By individual model (across all opponents)
    mdf = results.get("model_df", pd.DataFrame())
    if not mdf.empty:
        print("\nBy individual model (across all opponents):")
        by_model = mdf.groupby("model").agg(
            n_games=pd.NamedAgg(column="episode_uid", aggfunc="count"),
            fair_eff=pd.NamedAgg(column="fair_eff", aggfunc="mean"),
        ).round(3)
        print(by_model.to_string())
        print("  (fair_eff = reward / (collab_max/2))")

    # By mc_bucket
    if "mc_bucket" in gdf.columns and gdf["mc_bucket"].notna().any():
        print("\nBy M/C bucket:")
        by_mc = gdf.groupby("mc_bucket").agg(
            n=pd.NamedAgg(column="episode_uid", aggfunc="count"),
            joint_eff=pd.NamedAgg(column="allocation_efficiency", aggfunc="mean"),
            overdraw=pd.NamedAgg(column="overdraw_rate", aggfunc="mean"),
        ).round(3)
        print(by_mc.to_string())

    # Joint efficiency by round × model (pivot: models as columns, rounds as rows)
    rdf = results.get("round_df", pd.DataFrame())
    if not rdf.empty and "joint_efficiency" in rdf.columns and "model_a" in rdf.columns:
        print("\nJoint efficiency by round × model (mean):")
        by_round_model = (
            rdf.groupby(["round_number", "model_a"])["joint_efficiency"]
            .mean()
            .unstack("model_a")
            .round(3)
        )
        print(by_round_model.to_string())

        print("\nProportion of non-optimal allocations by round × model:")
        rdf_copy = rdf.copy()
        rdf_copy["non_optimal"] = rdf_copy["joint_efficiency"] < 1.0
        by_round_model_nonopt = (
            rdf_copy.groupby(["round_number", "model_a"])["non_optimal"]
            .mean()
            .unstack("model_a")
            .round(3)
        )
        print(by_round_model_nonopt.to_string())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compute outcome metrics for V5+ games")
    add_common_args(parser)
    args = parser.parse_args()
    dataset = load_dataset_from_args(args)
    print(f"Loaded {len(dataset.games)} games")
    results = compute_outcome_metrics(dataset)
    print_summary(results)
