"""RQ16: Perfunctory Fairness — Equal-Split Failure Mode.

Detects rounds where agents default to equal resource splits (e.g. both take
5 of the same resource) when that split is not jointly optimal. This is a
miscoordination failure mode where agents choose superficial fairness over
efficient specialization.

A round is "perfunctory fair" if, for every resource that both agents request,
they request equal quantities — and the round's joint efficiency is below the
optimal threshold.

Usage:
    uv run python -m scripts.analysis.rq16_perfunctory_fairness
"""

import numpy as np
import argparse
import pandas as pd

from negotiation_analysis.data_loader import add_common_args, load_dataset_from_args
from negotiation_analysis.models import NegotiationDataset


def analyze_perfunctory_fairness(
    dataset: NegotiationDataset, optimality_threshold: float = 0.95,
) -> dict:
    """Detect equal-split rounds and correlate with suboptimal outcomes.

    Args:
        dataset: NegotiationDataset to analyze.
        optimality_threshold: Joint efficiency above which a round is
            considered "optimal enough" (not a failure mode).

    Returns dict with:
        - round_df: per-round data with equal-split flags
        - by_model: perfunctory fairness rate per model
        - summary: aggregate stats
    """
    records = []

    for g in dataset.games:
        if g.schema_version < 5:
            continue
        resource_types = g.config.get("resource_types", [])

        for r in g.rounds:
            a_alloc = r.agent_a_resources
            b_alloc = r.agent_b_resources

            # Find resources both agents requested
            shared_resources = [
                res for res in resource_types
                if a_alloc.get(res, 0) > 0 and b_alloc.get(res, 0) > 0
            ]

            # Equal split: for every shared resource, quantities match
            has_equal_split = (
                len(shared_resources) > 0
                and all(
                    a_alloc.get(res, 0) == b_alloc.get(res, 0)
                    for res in shared_resources
                )
            )

            # Per-resource detail
            equal_split_resources = [
                res for res in shared_resources
                if a_alloc.get(res, 0) == b_alloc.get(res, 0)
            ]

            joint_eff = r.joint_efficiency
            is_suboptimal = (
                not np.isnan(joint_eff) and joint_eff < optimality_threshold
            )

            # Extract public speech for samples
            public_msgs = [
                {"speaker": e.speaker, "turn": e.turn_number, "message": e.content}
                for e in r.events
                if e.type == "speech" and e.speaker in ("agent_a", "agent_b")
            ]

            records.append({
                "episode_uid": g.episode_uid,
                "round_number": r.round_number,
                "model_a": g.model_a,
                "model_b": g.model_b,
                "pair": g.pair_name,
                "overdrawn": r.overdrawn,
                "num_shared_resources": len(shared_resources),
                "num_equal_splits": len(equal_split_resources),
                "has_equal_split": has_equal_split,
                "equal_split_resources": equal_split_resources,
                "joint_efficiency": joint_eff,
                "is_suboptimal": is_suboptimal,
                "perfunctory_fair": has_equal_split and is_suboptimal,
                "a_alloc": a_alloc,
                "b_alloc": b_alloc,
                "transcript": public_msgs,
            })

    round_df = pd.DataFrame(records)
    if round_df.empty:
        return {"round_df": round_df, "by_model": pd.DataFrame(), "summary": {}}

    # Per-model (melt: each round → 2 rows, one per agent's model)
    model_rows = []
    for _, row in round_df.iterrows():
        for model in set([row["model_a"], row["model_b"]]):
            model_rows.append({
                "model": model,
                "has_equal_split": row["has_equal_split"],
                "perfunctory_fair": row["perfunctory_fair"],
                "joint_efficiency": row["joint_efficiency"],
                "is_suboptimal": row["is_suboptimal"],
            })
    model_df = pd.DataFrame(model_rows)
    by_model = model_df.groupby("model").agg(
        n_rounds=pd.NamedAgg(column="has_equal_split", aggfunc="count"),
        equal_split_rate=pd.NamedAgg(column="has_equal_split", aggfunc="mean"),
        perfunctory_fair_rate=pd.NamedAgg(column="perfunctory_fair", aggfunc="mean"),
        mean_efficiency=pd.NamedAgg(column="joint_efficiency", aggfunc="mean"),
    ).round(3)

    # By pair
    by_pair = round_df.groupby("pair").agg(
        n_rounds=pd.NamedAgg(column="has_equal_split", aggfunc="count"),
        equal_split_rate=pd.NamedAgg(column="has_equal_split", aggfunc="mean"),
        perfunctory_fair_rate=pd.NamedAgg(column="perfunctory_fair", aggfunc="mean"),
        mean_efficiency=pd.NamedAgg(column="joint_efficiency", aggfunc="mean"),
    ).round(3)

    # Efficiency comparison: equal-split vs non-equal-split rounds
    eq_rounds = round_df[round_df["has_equal_split"]]
    neq_rounds = round_df[~round_df["has_equal_split"]]

    summary = {
        "n_rounds": len(round_df),
        "equal_split_rate": round_df["has_equal_split"].mean(),
        "perfunctory_fair_rate": round_df["perfunctory_fair"].mean(),
        "mean_eff_equal_split": eq_rounds["joint_efficiency"].mean() if len(eq_rounds) > 0 else np.nan,
        "mean_eff_specialized": neq_rounds["joint_efficiency"].mean() if len(neq_rounds) > 0 else np.nan,
        "optimality_threshold": optimality_threshold,
    }

    # Collect detailed samples of perfunctory fair rounds
    pf_rows = round_df[round_df["perfunctory_fair"]].copy()
    samples = []
    for _, row in pf_rows.iterrows():
        samples.append({
            "episode_uid": row["episode_uid"],
            "round_number": row["round_number"],
            "pair": row["pair"],
            "joint_efficiency": row["joint_efficiency"],
            "a_alloc": row["a_alloc"],
            "b_alloc": row["b_alloc"],
            "equal_split_resources": row["equal_split_resources"],
            "transcript": row["transcript"],
        })

    return {
        "round_df": round_df,
        "by_model": by_model,
        "by_pair": by_pair,
        "samples": samples,
        "summary": summary,
    }


def print_summary(results: dict) -> None:
    print("=" * 60)
    print("RQ16: Perfunctory Fairness (Equal-Split Failure Mode)")
    print("=" * 60)

    s = results["summary"]
    print(f"\nRounds analyzed: {s['n_rounds']}")
    print(f"Optimality threshold: {s['optimality_threshold']:.0%}")
    print(f"\nEqual-split rate: {s['equal_split_rate']:.1%}")
    print(f"Perfunctory fair rate (equal + suboptimal): {s['perfunctory_fair_rate']:.1%}")
    print(f"\nMean efficiency — equal splits: {s['mean_eff_equal_split']:.3f}")
    print(f"Mean efficiency — specialized:  {s['mean_eff_specialized']:.3f}")

    print("\nBy model:")
    print(results["by_model"].to_string())

    print("\nBy pair:")
    print(results["by_pair"].to_string())

    # Show samples with transcripts
    samples = results.get("samples", [])
    if samples:
        print(f"\nSample perfunctory fair rounds ({min(len(samples), 5)}/{len(samples)}):")
        for s in samples[:5]:
            print(f"\n  --- {s['episode_uid'][:8]} R{s['round_number']} "
                  f"[{s['pair']}] eff={s['joint_efficiency']:.2f} ---")
            print(f"  A alloc: {s['a_alloc']}")
            print(f"  B alloc: {s['b_alloc']}")
            print(f"  Equal splits on: {s['equal_split_resources']}")
            if s["transcript"]:
                print("  Transcript:")
                for turn in s["transcript"]:
                    speaker = turn["speaker"].replace("agent_", "").upper()
                    msg = turn["message"][:200]
                    print(f"    [{speaker}] {msg}")
            else:
                print("  (no cheap talk)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RQ16: Perfunctory Fairness")
    add_common_args(parser)
    args = parser.parse_args()
    dataset = load_dataset_from_args(args)
    results = analyze_perfunctory_fairness(dataset)
    print_summary(results)