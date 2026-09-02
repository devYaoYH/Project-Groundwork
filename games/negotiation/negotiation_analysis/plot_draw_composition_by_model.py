"""Plot per-model composition of eligible rounds by draw pattern (rebuttal figure).

Illustrates, for each model, the share of eligible rounds where both agents
drew <100%, one drew <100% while the other drew exactly 100%, and one drew
<100% while the other drew >100%. Counts and percentages are taken from the
rebuttal table; bars show percentages with "n (pct)" annotations.

Usage:
    uv run python -m scripts.analysis.plot_draw_composition_by_model
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

DEFAULT_FIG_DIR = Path("figures")

# Bar order within each model group.
CATEGORY_ORDER = [
    "Both <100%",
    "One <100%, one =100%",
    "One <100%, one >100%",
]

CATEGORY_COLORS = {
    "Both <100%": "#2f6f9f",
    "One <100%, one =100%": "#7da7c4",
    "One <100%, one >100%": "#b8c6d4",
}

TABLE_ROWS = [
    # model, eligible_rounds, both_lt, one_lt_one_gt, one_lt_one_eq
    ("GPT-5 Mini", 124, 47, 54, 23),
    ("Sonnet 4.5", 143, 58, 44, 41),
    ("GPT-OSS-120B", 129, 29, 63, 37),
    ("Gemini 3.1 Pro", 96, 50, 38, 8),
]


def build_table() -> pd.DataFrame:
    df = pd.DataFrame(
        TABLE_ROWS,
        columns=[
            "model",
            "eligible_rounds",
            "Both <100%",
            "One <100%, one >100%",
            "One <100%, one =100%",
        ],
    )
    long_df = df.melt(
        id_vars=["model", "eligible_rounds"],
        value_vars=CATEGORY_ORDER,
        var_name="category",
        value_name="count",
    )
    long_df["pct"] = 100.0 * long_df["count"] / long_df["eligible_rounds"]
    return long_df


def polish_axis(ax) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", color="#e8e8e8", linewidth=0.8)
    ax.set_axisbelow(True)


def plot_grouped_bars(df: pd.DataFrame, fig_dir: Path) -> Path:
    models = [row[0] for row in TABLE_ROWS]
    bar_width = 0.26

    fig, ax = plt.subplots(figsize=(9, 4.2))
    for cat_idx, category in enumerate(CATEGORY_ORDER):
        sub = df[df["category"] == category].set_index("model").loc[models]
        positions = [m_idx + (cat_idx - 1) * bar_width for m_idx in range(len(models))]
        ax.bar(
            positions,
            sub["count"],
            bar_width,
            color=CATEGORY_COLORS[category],
            label=category,
        )
        for pos, (_, row) in zip(positions, sub.iterrows()):
            ax.text(
                pos,
                row["count"] + 1.2,
                f"{row['count']}\n({row['pct']:.1f})",
                ha="center",
                va="bottom",
                fontsize=8,
            )

    eligible = {row[0]: row[1] for row in TABLE_ROWS}
    ax.set_xticks(range(len(models)))
    ax.set_xticklabels([f"{m}\n(n={eligible[m]})" for m in models])
    ax.set_ylabel("Number of rounds")
    ax.set_ylim(0, 76)
    ax.legend(
        frameon=False,
        loc="lower center",
        bbox_to_anchor=(0.5, 1.0),
        ncol=3,
        fontsize=9,
    )
    polish_axis(ax)
    fig.tight_layout()

    path = fig_dir / "draw_composition_by_model.png"
    fig.savefig(path, dpi=220)
    fig.savefig(path.with_suffix(".pdf"))
    plt.close(fig)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot per-model draw-pattern composition.")
    parser.add_argument("--fig-dir", type=Path, default=DEFAULT_FIG_DIR)
    args = parser.parse_args()

    args.fig_dir.mkdir(parents=True, exist_ok=True)
    df = build_table()
    print(plot_grouped_bars(df, args.fig_dir))


if __name__ == "__main__":
    main()
