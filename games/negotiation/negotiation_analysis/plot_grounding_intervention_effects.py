"""Plot grounding-intervention effects table (rebuttal figure).

Illustrates the no-prior-grounding vs. with-grounding comparison across
M/C levels for 1 - overdraw, joint efficiency, and optimum rate. Values
are taken from the paired-comparison table; deltas carry 95% CIs and
significance stars.

Usage:
    uv run python -m scripts.analysis.plot_grounding_intervention_effects
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

DEFAULT_FIG_DIR = Path("figures")

NO_GROUNDING_COLOR = "#b8c6d4"
GROUNDING_COLOR = "#2f6f9f"

TABLE_ROWS = [
    # metric, m_over_c, no_grounding, with_grounding, delta, ci_low, ci_high, p, stars
    ("1 - overdraw", 0.5, 62.1, 85.6, 23.5, 16.6, 30.7, 0.001, "***"),
    ("1 - overdraw", 0.8, 78.3, 88.1, 9.7, 3.7, 15.8, 0.002, "**"),
    ("1 - overdraw", 1.0, 89.6, 95.3, 5.7, 1.8, 9.8, 0.001, "**"),
    ("Joint efficiency", 0.5, 57.0, 76.8, 19.8, 13.2, 26.7, 0.001, "***"),
    ("Joint efficiency", 0.8, 72.2, 81.0, 8.8, 3.0, 14.7, 0.002, "**"),
    ("Joint efficiency", 1.0, 85.8, 90.0, 4.2, 0.2, 8.4, 0.039, "*"),
    ("Optimum rate", 0.5, 30.0, 34.4, 4.4, -2.8, 11.7, 0.238, ""),
    ("Optimum rate", 0.8, 52.1, 57.2, 5.1, -2.3, 13.0, 0.177, ""),
    ("Optimum rate", 1.0, 70.0, 68.3, -1.7, -9.5, 5.9, 0.669, ""),
]

METRIC_ORDER = ["1 - overdraw", "Joint efficiency", "Optimum rate"]


def build_table() -> pd.DataFrame:
    return pd.DataFrame(
        TABLE_ROWS,
        columns=[
            "metric",
            "m_over_c",
            "no_grounding",
            "with_grounding",
            "delta",
            "ci_low",
            "ci_high",
            "p",
            "stars",
        ],
    )


def polish_axis(ax) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", color="#e8e8e8", linewidth=0.8)
    ax.set_axisbelow(True)


def plot_grouped_bars(df: pd.DataFrame, fig_dir: Path) -> Path:
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.8), sharey=True)
    bar_width = 0.36

    for ax, metric in zip(axes, METRIC_ORDER):
        sub = df[df["metric"] == metric].sort_values("m_over_c")
        positions = range(len(sub))

        ax.bar(
            [p - bar_width / 2 for p in positions],
            sub["no_grounding"],
            bar_width,
            color=NO_GROUNDING_COLOR,
            label="First round",
        )
        ax.bar(
            [p + bar_width / 2 for p in positions],
            sub["with_grounding"],
            bar_width,
            color=GROUNDING_COLOR,
            label="Subsequent rounds",
        )

        for pos, (_, row) in zip(positions, sub.iterrows()):
            top = max(row["no_grounding"], row["with_grounding"])
            annotation = f"{row['delta']:+.1f}{row['stars']}"
            ax.text(pos, top + 2.5, annotation, ha="center", fontsize=10, fontweight="bold")
            ax.text(
                pos,
                top + 9.5,
                f"[{row['ci_low']:+.1f}, {row['ci_high']:+.1f}]",
                ha="center",
                fontsize=7.5,
                color="#555555",
            )

        ax.set_xticks(list(positions))
        ax.set_xticklabels([f"{v:.1f}" for v in sub["m_over_c"]])
        ax.set_xlabel("M/C")
        ax.set_title(metric)
        ax.set_ylim(0, 115)
        ax.set_yticks([0, 25, 50, 75, 100])
        polish_axis(ax)

    axes[0].set_ylabel("Rate (%)")
    axes[2].legend(frameon=False, loc="upper right", fontsize=8.5)
    fig.tight_layout()

    path = fig_dir / "grounding_intervention_effects.png"
    fig.savefig(path, dpi=220)
    fig.savefig(path.with_suffix(".pdf"))
    plt.close(fig)
    return path


def plot_delta_forest(df: pd.DataFrame, fig_dir: Path) -> Path:
    fig, ax = plt.subplots(figsize=(7, 3.6))

    rows = []
    for metric in METRIC_ORDER:
        sub = df[df["metric"] == metric].sort_values("m_over_c", ascending=False)
        rows.extend(sub.to_dict("records"))

    y_positions = []
    y_labels = []
    y = 0
    for i, row in enumerate(rows):
        if i > 0 and rows[i - 1]["metric"] != row["metric"]:
            y -= 0.6
        significant = bool(row["stars"])
        color = GROUNDING_COLOR if significant else "#9aa5ae"
        ax.errorbar(
            row["delta"],
            y,
            xerr=[[row["delta"] - row["ci_low"]], [row["ci_high"] - row["delta"]]],
            fmt="o",
            color=color,
            capsize=3,
            markersize=6,
        )
        label = f"{row['delta']:+.1f}{row['stars']}"
        ax.text(row["ci_high"] + 1.2, y, label, va="center", fontsize=8.5)
        y_positions.append(y)
        y_labels.append(f"{row['metric']}  (M/C={row['m_over_c']:.1f})")
        y -= 1

    ax.axvline(0, color="#333333", linewidth=1)
    ax.set_yticks(y_positions)
    ax.set_yticklabels(y_labels, fontsize=8.5)
    ax.set_xlabel("Grounding effect (percentage points, 95% CI)")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="x", color="#e8e8e8", linewidth=0.8)
    ax.set_axisbelow(True)
    fig.tight_layout()

    path = fig_dir / "grounding_intervention_delta_forest.png"
    fig.savefig(path, dpi=220)
    fig.savefig(path.with_suffix(".pdf"))
    plt.close(fig)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot grounding-intervention effects table.")
    parser.add_argument("--fig-dir", type=Path, default=DEFAULT_FIG_DIR)
    args = parser.parse_args()

    args.fig_dir.mkdir(parents=True, exist_ok=True)
    df = build_table()
    print(plot_grouped_bars(df, args.fig_dir))
    print(plot_delta_forest(df, args.fig_dir))


if __name__ == "__main__":
    main()
