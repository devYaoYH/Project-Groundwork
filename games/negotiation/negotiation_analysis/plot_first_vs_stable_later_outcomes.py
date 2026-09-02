"""Compare first-round outcomes against later stable-partner outcomes.

This mirrors the visual style of the paper's "Value of Cheap Talk" figure,
which was generated in notebooks/results.ipynb (Plot 7a) and saved as
plots/notalk_avg_value.png.

Usage:
    uv run python -m scripts.analysis.plot_first_vs_stable_later_outcomes
"""

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from negotiation_game.backend.defaults import REPO_ROOT
from negotiation_analysis.data_loader import (
    MAIN_COHORT_RUN_IDS,
    TOMBSTONED_GAME_IDS,
    load_dataset,
)
from negotiation_analysis.models import NegotiationDataset


MC_VALS = [0.5, 0.8, 1.0]
OPTIMUM_THRESHOLD = 0.999
OUTPUT_PATH = REPO_ROOT / "plots" / "first_vs_stable_later_outcomes.png"
LATEX_TABLE_PATH = REPO_ROOT / "plots" / "first_vs_stable_later_outcomes_table.tex"
DEFAULT_BOOTSTRAP_ITERS = 5000

METRICS = [
    ("inv_overdraw", "1 - Overdraw Rate (%)"),
    ("efficiency", "Joint Efficiency (%)"),
    ("optimum", "Optimum Rate (%)"),
]

COHORT_LABELS = {
    "first_round": "First round",
    "stable_later": "Stable later rounds",
    "stable_fixed_later": "Stable fixed later rounds",
}


def load_main_cohort_rounds() -> pd.DataFrame:
    """Load the 720-game paper main cohort and return one row per round."""
    dataset = load_dataset()
    games = [
        game
        for game in dataset.games
        if game.config.get("experiment_run_id") in MAIN_COHORT_RUN_IDS
        and not any(game.game_id.startswith(t) for t in TOMBSTONED_GAME_IDS)
    ]
    round_df = load_dataset_from_games(games).to_round_df()
    return round_df


def load_dataset_from_games(games) -> NegotiationDataset:
    """Wrap filtered games in a NegotiationDataset without reloading data."""
    return NegotiationDataset(games)


def summarize_cohort(df: pd.DataFrame, cohort: str) -> list[dict]:
    """Summarize outcome metrics by M/C bucket for a single cohort."""
    rows = []
    for mc in MC_VALS:
        sub = df[df["mc_bucket"] == mc]
        joint_efficiency = sub["joint_efficiency"].dropna()
        rows.append({
            "cohort": cohort,
            "mc": mc,
            "n_rounds": len(sub),
            "inv_overdraw": 100 * (1 - sub["overdrawn"].mean()) if len(sub) else float("nan"),
            "efficiency": 100 * joint_efficiency.mean() if len(joint_efficiency) else float("nan"),
            "optimum": 100 * (joint_efficiency >= OPTIMUM_THRESHOLD).mean()
            if len(joint_efficiency) else float("nan"),
        })
    return rows


def summarize_single_metric(df: pd.DataFrame, metric: str) -> float:
    """Compute a single outcome metric on a round subset as percentage points."""
    if df.empty:
        return float("nan")
    if metric == "inv_overdraw":
        return 100 * (1 - df["overdrawn"].mean())

    joint_efficiency = df["joint_efficiency"].dropna()
    if joint_efficiency.empty:
        return float("nan")
    if metric == "efficiency":
        return 100 * joint_efficiency.mean()
    if metric == "optimum":
        return 100 * (joint_efficiency >= OPTIMUM_THRESHOLD).mean()
    raise ValueError(f"Unknown metric: {metric}")


def build_comparison_df(round_df: pd.DataFrame) -> pd.DataFrame:
    """Build requested cohorts from the main-cohort round table."""
    first_round = round_df[round_df["round_number"] == 1].copy()
    stable_later = round_df[
        (round_df["round_number"] > 1)
        & (round_df["mode"] == "stable")
    ].copy()
    stable_fixed_later = round_df[
        (round_df["round_number"] > 1)
        & (round_df["mode"] == "stable")
        & ~round_df["is_rotating"]
    ].copy()

    rows = []
    rows.extend(summarize_cohort(first_round, "first_round"))
    rows.extend(summarize_cohort(stable_later, "stable_later"))
    rows.extend(summarize_cohort(stable_fixed_later, "stable_fixed_later"))
    return pd.DataFrame(rows)


def bootstrap_differences(
    round_df: pd.DataFrame,
    comparison_cohorts: tuple[str, ...] = ("stable_later", "stable_fixed_later"),
    n_bootstrap: int = 5000,
    seed: int = 0,
) -> pd.DataFrame:
    """Cluster-bootstrap cohort differences by resampling games within M/C bucket.

    The compared cohorts share some games, so resampling at the game level keeps
    within-game round dependence intact.
    """
    rng = np.random.default_rng(seed)
    per_game_df = build_game_level_metrics(round_df, comparison_cohorts)
    rows = []

    for mc in MC_VALS:
        mc_df = per_game_df[per_game_df["mc"] == mc].reset_index(drop=True)
        row_indices = np.arange(len(mc_df))
        observed = compute_difference_rows_from_game_metrics(mc_df, mc, comparison_cohorts)

        bootstrap_values: dict[tuple[str, str], list[float]] = {
            (row["comparison"], row["metric"]): []
            for row in observed
        }

        for _ in range(n_bootstrap):
            sampled_df = mc_df.iloc[rng.choice(row_indices, size=len(row_indices), replace=True)]
            for row in compute_difference_rows_from_game_metrics(sampled_df, mc, comparison_cohorts):
                bootstrap_values[(row["comparison"], row["metric"])].append(row["diff_pp"])

        for row in observed:
            values = np.array(bootstrap_values[(row["comparison"], row["metric"])])
            values = values[~np.isnan(values)]
            rows.append({
                **row,
                "ci_low": np.percentile(values, 2.5),
                "ci_high": np.percentile(values, 97.5),
                "p_higher": np.mean(values > 0),
                "p_two_sided": min(1.0, 2 * min(np.mean(values <= 0), np.mean(values >= 0))),
                "n_bootstrap": len(values),
            })

    return pd.DataFrame(rows)


def bootstrap_point_intervals(
    round_df: pd.DataFrame,
    plotted_cohorts: tuple[str, ...] = ("first_round", "stable_later"),
    n_bootstrap: int = DEFAULT_BOOTSTRAP_ITERS,
    seed: int = 0,
) -> pd.DataFrame:
    """Cluster-bootstrap 95% CIs for each plotted metric point."""
    rng = np.random.default_rng(seed)
    per_game_df = build_game_level_metrics(round_df, plotted_cohorts[1:])
    rows = []

    for mc in MC_VALS:
        mc_df = per_game_df[per_game_df["mc"] == mc].reset_index(drop=True)
        row_indices = np.arange(len(mc_df))
        for cohort in plotted_cohorts:
            for metric, _ in METRICS:
                observed = mc_df[f"{cohort}_{metric}"].mean()
                values = []
                for _ in range(n_bootstrap):
                    sampled_df = mc_df.iloc[rng.choice(row_indices, size=len(row_indices), replace=True)]
                    values.append(sampled_df[f"{cohort}_{metric}"].mean())
                values_array = np.array(values)
                values_array = values_array[~np.isnan(values_array)]
                rows.append({
                    "cohort": cohort,
                    "mc": mc,
                    "metric": metric,
                    "value": observed,
                    "ci_low": np.percentile(values_array, 2.5),
                    "ci_high": np.percentile(values_array, 97.5),
                })

    return pd.DataFrame(rows)


def build_game_level_metrics(
    round_df: pd.DataFrame,
    comparison_cohorts: tuple[str, ...],
) -> pd.DataFrame:
    """Collapse rounds to one row per game for clustered bootstrapping."""
    rows = []
    for game_id, game_df in round_df.groupby("game_id"):
        row = {
            "game_id": game_id,
            "mc": game_df["mc_bucket"].iloc[0],
        }
        cohort_frames = {
            "first_round": game_df[game_df["round_number"] == 1],
            "stable_later": game_df[
                (game_df["round_number"] > 1)
                & (game_df["mode"] == "stable")
            ],
            "stable_fixed_later": game_df[
                (game_df["round_number"] > 1)
                & (game_df["mode"] == "stable")
                & ~game_df["is_rotating"]
            ],
        }
        for cohort in ("first_round", *comparison_cohorts):
            for metric, _ in METRICS:
                row[f"{cohort}_{metric}"] = summarize_single_metric(cohort_frames[cohort], metric)
            row[f"{cohort}_n_rounds"] = len(cohort_frames[cohort])
        rows.append(row)
    return pd.DataFrame(rows)


def compute_difference_rows_from_game_metrics(
    game_metric_df: pd.DataFrame,
    mc: float,
    comparison_cohorts: tuple[str, ...],
) -> list[dict]:
    """Compute observed cohort-minus-first-round differences from game metrics."""

    rows = []
    for comparison in comparison_cohorts:
        for metric, _ in METRICS:
            first_value = game_metric_df[f"first_round_{metric}"].mean()
            comparison_value = game_metric_df[f"{comparison}_{metric}"].mean()
            rows.append({
                "comparison": comparison,
                "mc": mc,
                "metric": metric,
                "first_round": first_value,
                "comparison_value": comparison_value,
                "diff_pp": comparison_value - first_value,
                "n_first_rounds": int(game_metric_df["first_round_n_rounds"].sum()),
                "n_comparison_rounds": int(game_metric_df[f"{comparison}_n_rounds"].sum()),
            })
    return rows


def plot_comparison(
    summary_df: pd.DataFrame,
    interval_df: pd.DataFrame,
    output_path: Path = OUTPUT_PATH,
) -> None:
    """Plot first-round vs stable-later outcomes in the cheap-talk figure style."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(1, 3, figsize=(14, 4), sharey=False)

    first = summary_df[summary_df["cohort"] == "first_round"].sort_values("mc")
    stable = summary_df[summary_df["cohort"] == "stable_later"].sort_values("mc")

    for ax, (metric, title) in zip(axes, METRICS):
        first_intervals = interval_df[
            (interval_df["cohort"] == "first_round")
            & (interval_df["metric"] == metric)
        ].sort_values("mc")
        stable_intervals = interval_df[
            (interval_df["cohort"] == "stable_later")
            & (interval_df["metric"] == metric)
        ].sort_values("mc")

        ax.errorbar(
            first["mc"],
            first[metric],
            yerr=[
                first[metric].to_numpy() - first_intervals["ci_low"].to_numpy(),
                first_intervals["ci_high"].to_numpy() - first[metric].to_numpy(),
            ],
            marker="o",
            linewidth=2.2,
            markersize=8,
            capsize=4,
            elinewidth=1.2,
            color="#2c7bb6",
            label=COHORT_LABELS["first_round"],
        )
        ax.errorbar(
            stable["mc"],
            stable[metric],
            yerr=[
                stable[metric].to_numpy() - stable_intervals["ci_low"].to_numpy(),
                stable_intervals["ci_high"].to_numpy() - stable[metric].to_numpy(),
            ],
            marker="o",
            linewidth=2.2,
            markersize=8,
            capsize=4,
            elinewidth=1.2,
            color="#d7191c",
            linestyle="--",
            label=COHORT_LABELS["stable_later"],
        )
        ax.fill_between(first["mc"], first[metric], stable[metric], alpha=0.12, color="#2c7bb6")

        ax.set_title(title, fontsize=11)
        ax.set_xlabel("M/C Ratio", fontsize=10)
        ax.set_ylabel("%" if ax == axes[0] else "", fontsize=10)
        ax.set_xticks(MC_VALS)
        ax.set_xlim(0.4, 1.1)
        ax.set_ylim(20, 105)
        ax.grid(True, axis="y", alpha=0.3)

    axes[0].legend(fontsize=9, frameon=True)
    fig.suptitle("First Round vs Stable Later Rounds across M/C Ratios", fontsize=13, y=1.02)
    fig.tight_layout()
    fig.savefig(output_path, bbox_inches="tight", dpi=150)
    plt.close(fig)


def print_summary(summary_df: pd.DataFrame) -> None:
    """Print compact numeric values for auditability."""
    display = summary_df.copy()
    for metric, _ in METRICS:
        display[metric] = display[metric].round(1)
    print(display[["cohort", "mc", "n_rounds", "inv_overdraw", "efficiency", "optimum"]].to_string(index=False))


def print_bootstrap_summary(bootstrap_df: pd.DataFrame) -> None:
    """Print bootstrap confidence estimates for later-minus-first differences."""
    display = bootstrap_df.copy()
    for col in ["first_round", "comparison_value", "diff_pp", "ci_low", "ci_high", "p_higher", "p_two_sided"]:
        display[col] = display[col].round(3)
    display["metric"] = display["metric"].map({
        "inv_overdraw": "1-overdraw",
        "efficiency": "efficiency",
        "optimum": "optimum",
    })
    print("\nBootstrap differences: comparison cohort minus first round (percentage points)")
    print(
        display[
            [
                "comparison",
                "mc",
                "metric",
                "diff_pp",
                "ci_low",
                "ci_high",
                "p_higher",
                "p_two_sided",
                "n_first_rounds",
                "n_comparison_rounds",
            ]
        ].to_string(index=False)
    )


def format_p_value(p_value: float) -> str:
    """Format a p-value for a compact paper table."""
    if pd.isna(p_value):
        return "--"
    if p_value < 0.001:
        return "$<0.001$"
    return f"{p_value:.3f}"


def significance_stars(p_value: float) -> str:
    """Return conventional significance stars for a two-sided p-value."""
    if pd.isna(p_value):
        return ""
    if p_value < 0.001:
        return "***"
    if p_value < 0.01:
        return "**"
    if p_value < 0.05:
        return "*"
    return ""


def latex_metric_label(metric: str) -> str:
    """Map internal metric names to LaTeX table labels."""
    labels = {
        "inv_overdraw": "1 - overdraw",
        "efficiency": "Joint efficiency",
        "optimum": "Optimum rate",
    }
    return labels[metric]


def build_latex_table(
    bootstrap_df: pd.DataFrame,
    comparison: str = "stable_later",
) -> str:
    """Build a LaTeX tabular fragment for the plotted comparison."""
    rows = [
        "% Auto-generated by scripts.analysis.plot_first_vs_stable_later_outcomes",
        "% Test: game-cluster bootstrap over games within each M/C bucket.",
        "\\begin{tabular}{llrrrr}",
        "\\toprule",
        "$M/C$ & Metric & First round & Stable later & $\\Delta$ [95\\% CI] & $p$ \\\\",
        "\\midrule",
    ]
    table_df = bootstrap_df[bootstrap_df["comparison"] == comparison].copy()
    metric_order = {metric: idx for idx, (metric, _) in enumerate(METRICS)}
    table_df["metric_order"] = table_df["metric"].map(metric_order)
    table_df = table_df.sort_values(["mc", "metric_order"])

    for _, row in table_df.iterrows():
        p_value = row["p_two_sided"]
        delta_text = (
            f"{row['diff_pp']:+.1f}"
            f"{significance_stars(p_value)}"
            f" [{row['ci_low']:+.1f}, {row['ci_high']:+.1f}]"
        )
        rows.append(
            f"{row['mc']:.1f} & "
            f"{latex_metric_label(row['metric'])} & "
            f"{row['first_round']:.1f} & "
            f"{row['comparison_value']:.1f} & "
            f"{delta_text} & "
            f"{format_p_value(p_value)} \\\\"
        )

    rows.extend([
        "\\bottomrule",
        "\\end{tabular}",
        "% Values are percentages. Delta is stable later minus first round in percentage points.",
        "% Significance stars use the two-sided game-cluster bootstrap p-value: * p<0.05, ** p<0.01, *** p<0.001.",
    ])
    return "\n".join(rows) + "\n"


def write_latex_table(
    bootstrap_df: pd.DataFrame,
    output_path: Path = LATEX_TABLE_PATH,
) -> None:
    """Write the LaTeX table fragment to disk."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(build_latex_table(bootstrap_df))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plot first-round outcomes vs later stable-partner outcomes for the main cohort.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help=f"Output image path (default: {OUTPUT_PATH})",
    )
    parser.add_argument(
        "--table-output",
        type=Path,
        default=LATEX_TABLE_PATH,
        help=f"Output LaTeX table path (default: {LATEX_TABLE_PATH})",
    )
    parser.add_argument(
        "--bootstrap-iters",
        type=int,
        default=DEFAULT_BOOTSTRAP_ITERS,
        help="Number of clustered bootstrap iterations for confidence estimates.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Random seed for bootstrap resampling.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    round_df = load_main_cohort_rounds()
    summary_df = build_comparison_df(round_df)
    interval_df = bootstrap_point_intervals(
        round_df,
        n_bootstrap=args.bootstrap_iters,
        seed=args.seed,
    )
    plot_comparison(summary_df, interval_df, args.output)
    print_summary(summary_df)
    bootstrap_df = bootstrap_differences(
        round_df,
        n_bootstrap=args.bootstrap_iters,
        seed=args.seed,
    )
    print_bootstrap_summary(bootstrap_df)
    write_latex_table(bootstrap_df, args.table_output)
    print(f"Saved -> {args.output}")
    print(f"Saved LaTeX table -> {args.table_output}")


if __name__ == "__main__":
    main()
