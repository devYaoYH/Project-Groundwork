"""RQ5: Speed of Settling on Global Optimum (Collaborative).

In collaborative (different-value) conditions, how quickly do agents converge
on the allocation that maximises joint reward?

Usage:
    uv run python scripts/analysis/rq5_global_optimum.py
"""

import json
import argparse
from itertools import combinations

import numpy as np
import pandas as pd

from negotiation_analysis.data_loader import add_common_args, load_dataset_from_args, BUDGET, RESOURCE_COSTS, RESOURCE_SUPPLY
from negotiation_analysis.models import NegotiationDataset


def _feasible_allocations(
    costs: dict[str, float] = RESOURCE_COSTS,
    supply: dict[str, int] = RESOURCE_SUPPLY,
    budget: float = BUDGET,
) -> list[dict[str, int]]:
    """Generate all feasible single-agent allocations (at most 2 resource types)."""
    resources = list(costs.keys())
    allocs = [{}]  # empty allocation
    for n_types in range(1, 3):
        for combo in combinations(resources, n_types):
            max_qtys = [
                min(supply[r], int(budget // costs[r]))
                for r in combo
            ]
            if len(combo) == 1:
                for q in range(0, max_qtys[0] + 1):
                    if q * costs[combo[0]] <= budget:
                        allocs.append({combo[0]: q})
            else:
                for q1 in range(0, max_qtys[0] + 1):
                    for q2 in range(0, max_qtys[1] + 1):
                        cost = (
                            q1 * costs[combo[0]]
                            + q2 * costs[combo[1]]
                        )
                        if cost <= budget:
                            allocs.append({combo[0]: q1, combo[1]: q2})
    return allocs


def compute_optimal_joint_reward(
    a_vals: dict,
    b_vals: dict,
    costs: dict[str, float] = RESOURCE_COSTS,
    supply: dict[str, int] = RESOURCE_SUPPLY,
    budget: float = BUDGET,
) -> tuple[dict, dict, float]:
    """Brute-force search for the joint-reward-maximising allocation pair."""
    resources = list(costs.keys())
    all_allocs = _feasible_allocations(costs, supply, budget)
    best_reward = -1.0
    best_a: dict = {}
    best_b: dict = {}

    for a_alloc in all_allocs:
        for b_alloc in all_allocs:
            overdrawn = any(
                a_alloc.get(r, 0) + b_alloc.get(r, 0) > supply[r]
                for r in resources
            )
            if overdrawn:
                continue
            a_reward = sum(
                a_alloc.get(r, 0) * a_vals.get(r, 0) for r in resources
            )
            b_reward = sum(
                b_alloc.get(r, 0) * b_vals.get(r, 0) for r in resources
            )
            joint = a_reward + b_reward
            if joint > best_reward:
                best_reward = joint
                best_a = a_alloc.copy()
                best_b = b_alloc.copy()

    return best_a, best_b, best_reward


def analyze_optimum_convergence(dataset: NegotiationDataset) -> dict:
    """Measure how quickly games converge to the optimal allocation.

    Supports both V1-V4 (value-function) and V5+ (project-based with oracle stats).
    V1-V4: filters to collaborative games only, brute-force optimal.
    V5+: uses precomputed oracle_stats.collab_max (all games, M/C ratio captures competitiveness).
    """
    # Separate V5+ and legacy games
    v5_games = [g for g in dataset.games if g.schema_version >= 5 and g.num_rounds >= 2]
    legacy_games = [
        g for g in dataset.games
        if g.schema_version < 5 and g.num_rounds >= 2
    ]

    # Cache optimal reward per value-pair + cost combination (legacy only)
    cache: dict[tuple[str, str, str], tuple[dict, dict, float]] = {}
    records = []

    # V5+ games: use precomputed oracle stats
    for g in v5_games:
        pair = g.pair_name
        for r in g.rounds:
            oracle = r.oracle_stats
            opt_reward = oracle.get("collab_max", 0) if oracle else 0
            actual = 0 if r.overdrawn else r.joint_reward
            records.append({
                "episode_uid": g.episode_uid,
                "mode": g.mode,
                "model_a": g.model_a,
                "model_b": g.model_b,
                "pair": pair,
                "round_number": r.round_number,
                "actual_joint_reward": actual,
                "optimal_joint_reward": opt_reward,
                "optimality_ratio": actual / opt_reward if opt_reward > 0 else 0,
                "overdrawn": r.overdrawn,
                "mc_ratio": oracle.get("mc_ratio") if oracle else None,
                "schema_version": g.schema_version,
            })

    # Legacy games: brute-force optimal
    for g in legacy_games:
        agent_values = g.config.get("agent_values", [{}, {}])
        a_vals = agent_values[0] if len(agent_values) > 0 else {}
        b_vals = agent_values[1] if len(agent_values) > 1 else {}
        costs = g.config.get("resource_costs", RESOURCE_COSTS)
        budget = g.config.get("agent_budget", BUDGET)
        key = (
            json.dumps(a_vals, sort_keys=True),
            json.dumps(b_vals, sort_keys=True),
            json.dumps(costs, sort_keys=True),
        )
        if key not in cache:
            cache[key] = compute_optimal_joint_reward(
                a_vals, b_vals, costs=costs, budget=budget,
            )
        _, _, opt_reward = cache[key]

        for r in g.rounds:
            actual = 0 if r.overdrawn else r.joint_reward
            records.append({
                "episode_uid": g.episode_uid,
                "mode": g.mode,
                "model_a": "legacy",
                "model_b": "legacy",
                "pair": "legacy",
                "round_number": r.round_number,
                "actual_joint_reward": actual,
                "optimal_joint_reward": opt_reward,
                "optimality_ratio": actual / opt_reward if opt_reward > 0 else 0,
                "overdrawn": r.overdrawn,
                "mc_ratio": None,
                "schema_version": g.schema_version,
            })

    opt_df = pd.DataFrame(records)

    # Rounds to first reach threshold
    threshold = 0.9
    rounds_to_opt = []
    for gid, grp in opt_df.sort_values("round_number").groupby("episode_uid"):
        first_good = grp[grp["optimality_ratio"] >= threshold]["round_number"]
        rounds_to_opt.append({
            "episode_uid": gid,
            "round_to_optimal": first_good.iloc[0] if len(first_good) > 0 else np.nan,
            "mode": grp["mode"].iloc[0],
        })
    rto_df = pd.DataFrame(rounds_to_opt)

    return {
        "opt_df": opt_df,
        "rto_df": rto_df,
        "threshold": threshold,
        "value_pair_cache": cache,
    }


def print_summary(results: dict) -> None:
    print("=" * 60)
    print("RQ5: Speed of Settling on Global Optimum")
    print("=" * 60)

    opt_df = results["opt_df"]
    print("\nMean optimality ratio by round:")
    print(
        opt_df.groupby("round_number")["optimality_ratio"]
        .describe()
        .round(3)
    )

    rto_df = results["rto_df"]
    reached = rto_df["round_to_optimal"].notna().sum()
    total = len(rto_df)
    print(f"\nGames reaching {results['threshold']:.0%} optimality: {reached}/{total}")
    if reached > 0:
        print(
            f"Mean rounds to reach: "
            f"{rto_df['round_to_optimal'].dropna().mean():.1f}"
        )

    print("\nOptimal joint rewards per value pair:")
    for key, (a, b, reward) in results["value_pair_cache"].items():
        print(f"  {key[0]} / {key[1]}")
        print(f"    Optimal: A={a}, B={b}, reward={reward}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RQ5: Global Optimum Convergence")
    add_common_args(parser)
    args = parser.parse_args()
    dataset = load_dataset_from_args(args)
    results = analyze_optimum_convergence(dataset)
    print_summary(results)