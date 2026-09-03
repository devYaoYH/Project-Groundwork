"""RQ10: Allocation Efficiency.

Compare each agent's actual reward to theoretical maximum benchmarks:
- Unconstrained individual max: ignoring supply limits and opponent
- Constrained individual max: respecting supply limits, ignoring opponent
- Joint optimal: max combined reward for both agents cooperating

In shifting mode, only agent context resets between rounds — value functions
remain stable for both agents, so all efficiency metrics are computable.

Usage:
    uv run python -m scripts.analysis.rq10_efficiency
"""

import json
import argparse
from itertools import combinations

import numpy as np
import pandas as pd

from negotiation_analysis.data_loader import add_common_args, load_games_from_args, load_experiment_data, build_round_df, RESOURCE_COSTS, RESOURCE_SUPPLY, BUDGET, RESOURCES
from negotiation_analysis.rq5_global_optimum import compute_optimal_joint_reward
from scripts.export_traces import compute_theoretical_max

DEFAULT_MAX_TYPES = 2


def _feasible_single_agent_allocations(
    costs: dict[str, float] = RESOURCE_COSTS,
    supply: dict[str, int] = RESOURCE_SUPPLY,
    budget: float = BUDGET,
    max_types: int = DEFAULT_MAX_TYPES,
    resources: list[str] = RESOURCES,
) -> list[dict[str, int]]:
    """Generate all feasible single-agent allocations respecting supply."""
    allocs = [{}]
    for n_types in range(1, max_types + 1):
        for combo in combinations(resources, n_types):
            max_qtys = [
                min(supply[r], int(budget // costs[r])) for r in combo
            ]
            if len(combo) == 1:
                for q in range(1, max_qtys[0] + 1):
                    if q * costs[combo[0]] <= budget:
                        allocs.append({combo[0]: q})
            else:
                for q1 in range(0, max_qtys[0] + 1):
                    for q2 in range(0, max_qtys[1] + 1):
                        if q1 == 0 and q2 == 0:
                            continue
                        cost = q1 * costs[combo[0]] + q2 * costs[combo[1]]
                        if cost <= budget:
                            allocs.append({combo[0]: q1, combo[1]: q2})
    return allocs


def compute_constrained_individual_max(
    values: dict[str, float],
    costs: dict[str, float] = RESOURCE_COSTS,
    supply: dict[str, int] = RESOURCE_SUPPLY,
    budget: float = BUDGET,
    max_types: int = DEFAULT_MAX_TYPES,
) -> float:
    """Best reward for one agent respecting supply limits but ignoring opponent."""
    best = 0.0
    for alloc in _feasible_single_agent_allocations(costs, supply, budget, max_types):
        reward = sum(alloc.get(r, 0) * values.get(r, 0) for r in RESOURCES)
        if reward > best:
            best = reward
    return best


def analyze_efficiency(raw_games: list[dict]) -> dict:
    """Compute per-round and per-environment efficiency metrics.

    Supports V1-V4 (value-function) and V5+ (project-based with oracle stats).
    """
    optimal_cache: dict[str, tuple[dict, dict, float]] = {}
    records = []

    for g in raw_games:
        mode = g["mode"]
        schema_version = g.get("schema_version", 1)
        model_a, model_b = get_agent_models(g)

        for r in g["rounds"]:
            overdrawn = r.get("overdrawn", False)
            a_reward = 0.0 if overdrawn else r.get("agent_a_reward", 0)
            b_reward = 0.0 if overdrawn else r.get("agent_b_reward", 0)
            joint_reward = a_reward + b_reward

            if schema_version >= 5:
                # V5+: use precomputed oracle stats
                round_idx = r["round_number"] - 1
                oracle = get_round_oracle(g, round_idx)
                a_unconstrained = oracle.get("v1", 0) if oracle else 0
                b_unconstrained = oracle.get("v2", 0) if oracle else 0
                joint_opt = oracle.get("collab_max", 0) if oracle else 0
                a_constrained = a_unconstrained  # v1/v2 are solo maxima
                b_constrained = b_unconstrained
                mc_ratio = oracle.get("mc_ratio") if oracle else None
            else:
                # V1-V4: compute from value functions
                a_vals = g["agent_a_values"]
                b_vals = g["agent_b_values"]
                costs = g.get("resource_costs", RESOURCE_COSTS)
                budget = g.get("agent_budget", BUDGET)
                max_types = DEFAULT_MAX_TYPES

                a_unconstrained = compute_theoretical_max(a_vals, costs, budget, max_types)
                a_constrained = compute_constrained_individual_max(
                    a_vals, costs, RESOURCE_SUPPLY, budget, max_types,
                )
                b_unconstrained = compute_theoretical_max(b_vals, costs, budget, max_types)
                b_constrained = compute_constrained_individual_max(
                    b_vals, costs, RESOURCE_SUPPLY, budget, max_types,
                )
                key = (
                    json.dumps(a_vals, sort_keys=True),
                    json.dumps(b_vals, sort_keys=True),
                    json.dumps(costs, sort_keys=True),
                )
                if key not in optimal_cache:
                    optimal_cache[key] = compute_optimal_joint_reward(
                        a_vals, b_vals, costs=costs, budget=budget,
                    )
                _, _, joint_opt = optimal_cache[key]
                mc_ratio = None

            records.append({
                "episode_uid": g["episode_uid"],
                "round_number": r["round_number"],
                "condition": g["condition"],
                "mode": mode,
                "goal_type": g["goal_type"],
                "model_a": model_a,
                "model_b": model_b,
                "pair": f"{model_a} vs {model_b}" if model_a != model_b else model_a,
                "schema_version": schema_version,
                "mc_ratio": mc_ratio,
                "overdrawn": overdrawn,
                "agent_a_reward": a_reward,
                "agent_b_reward": b_reward,
                "joint_reward": joint_reward,
                "agent_a_unconstrained_max": a_unconstrained,
                "agent_a_constrained_max": a_constrained,
                "agent_b_unconstrained_max": b_unconstrained,
                "agent_b_constrained_max": b_constrained,
                "joint_optimal": joint_opt,
                "agent_a_eff": a_reward / a_constrained if a_constrained > 0 else 0,
                "agent_b_eff": b_reward / b_constrained if b_constrained > 0 else np.nan,
                "joint_efficiency": joint_reward / joint_opt if joint_opt > 0 else np.nan,
            })

    efficiency_df = pd.DataFrame(records)

    # Per-environment summary
    game_groups = efficiency_df.groupby(["episode_uid", "condition", "mode", "goal_type"])
    game_summary_df = game_groups.agg(
        total_a_reward=pd.NamedAgg(column="agent_a_reward", aggfunc="sum"),
        total_b_reward=pd.NamedAgg(column="agent_b_reward", aggfunc="sum"),
        total_joint_reward=pd.NamedAgg(column="joint_reward", aggfunc="sum"),
        mean_agent_a_eff=pd.NamedAgg(column="agent_a_eff", aggfunc="mean"),
        mean_agent_b_eff=pd.NamedAgg(column="agent_b_eff", aggfunc="mean"),
        mean_joint_efficiency=pd.NamedAgg(column="joint_efficiency", aggfunc="mean"),
        overdraw_count=pd.NamedAgg(column="overdrawn", aggfunc="sum"),
        num_rounds=pd.NamedAgg(column="round_number", aggfunc="count"),
    ).reset_index()

    # Condition-level means
    eff_cols = ["agent_a_eff", "agent_b_eff", "joint_efficiency"]
    by_condition = efficiency_df.groupby("condition")[eff_cols].mean()

    # Convergence: efficiency over rounds by condition
    by_condition_round = (
        efficiency_df.groupby(["condition", "round_number"])[eff_cols]
        .agg(["mean", "sem"])
    )
    by_condition_round.columns = [
        f"{col}_{stat}" for col, stat in by_condition_round.columns
    ]
    by_condition_round = by_condition_round.reset_index()

    return {
        "efficiency_df": efficiency_df,
        "game_summary_df": game_summary_df,
        "by_condition": by_condition,
        "by_condition_round": by_condition_round,
    }


def print_summary(results: dict) -> None:
    print("=" * 60)
    print("RQ10: Allocation Efficiency")
    print("=" * 60)

    print("\nMean efficiency by condition:")
    print(results["by_condition"].round(3).to_string())

    print("\nPer-environment summary:")
    gs = results["game_summary_df"]
    for _, row in gs.iterrows():
        print(
            f"  {row['episode_uid'][:8]} ({row['condition']}): "
            f"joint_eff={row['mean_joint_efficiency']:.3f}, "
            f"a_eff={row['mean_agent_a_eff']:.3f}, "
            f"overdraws={int(row['overdraw_count'])}/{int(row['num_rounds'])}"
        )

    # Efficiency loss: competitive vs collaborative
    bc = results["by_condition"]
    for metric in ["agent_a_eff", "joint_efficiency"]:
        collab = bc.loc[bc.index.str.contains("collaborative"), metric].mean()
        comp = bc.loc[bc.index.str.contains("competitive"), metric].mean()
        if not np.isnan(collab) and not np.isnan(comp):
            print(f"\n{metric} — collaborative: {collab:.3f}, competitive: {comp:.3f}, "
                  f"loss: {collab - comp:.3f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RQ10: Allocation Efficiency")
    add_common_args(parser)
    args = parser.parse_args()
    raw_games = load_games_from_args(args)
    results = analyze_efficiency(raw_games)
    print_summary(results)