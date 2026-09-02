"""Plot Section 5.5 anchoring and first-proposal deference failure modes.

Illustrates the two failure modes from the paper section "Anchoring and
proposal deference": (A) stubborn anchoring, where dyads repeat a prior
suboptimal allocation they could have corrected; (B) first-proposal
deference, where accepting an uninformed opening proposal locks dyads into
suboptimal outcomes about as often as optimal ones; (C) per-model proposal
acceptance rates. Values are taken from the paper text.

Usage:
    uv run python -m scripts.analysis.plot_anchoring_deference_failures
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt

DEFAULT_FIG_DIR = Path("figures")

BLUE = "#2f6f9f"
LIGHT_BLUE = "#b8c6d4"
RED = "#9d4b4b"

# Panel A: allocation repeat rates, then stubborn-anchor rates among the 339
# suboptimal non-overdrawn (correctable) rounds. Listed top-to-bottom.
REPEAT_ROWS = [
    ("All same-scenario rounds", 46.5),
    ("After non-overdrawn round", 55.1),
    ("After overdrawn round", 0.6),
]
STUBBORN_ROWS = [
    ("All correctable failures", 29.2),
    ("Stable games", 40.7),
    ("Shifting games", 15.9),
    ("M/C = 1.0", 46.0),
    ("M/C = 0.5", 28.3),
]

# Panel B: share of exact-match deferences ending suboptimal vs. optimal.
DEFERENCE_GROUPS = [
    ("Round 1\n(non-rotating)", 8.2, 12.2),
    ("All rounds\n(non-rotating)", 15.5, 16.9),
    ("All rounds\n(rotating)", 20.9, 14.4),
]

# Panel C: share of explicit first proposals accepted, by model.
COMPLIANCE_ROWS = [
    ("Sonnet 4.5", 44.0),
    ("Qwen 3.5 Flash", 32.0),
    ("GPT-5 Mini", 21.0),
]


def polish_axis(ax, grid_axis: str) -> None:
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis=grid_axis, color="#e8e8e8", linewidth=0.8)
    ax.set_axisbelow(True)


def draw_anchoring_panel(ax) -> None:
    rows = []
    for label, value in REPEAT_ROWS:
        rows.append((label, value, LIGHT_BLUE))
    rows.append((None, None, None))
    for label, value in STUBBORN_ROWS:
        rows.append((label, value, BLUE))

    y = 0
    y_ticks = []
    y_labels = []
    seen_colors = set()
    for label, value, color in rows:
        if label is None:
            y -= 0.5
            continue
        y -= 1
        legend_label = None
        if color not in seen_colors:
            seen_colors.add(color)
            legend_label = (
                "Allocation repeat rate"
                if color == LIGHT_BLUE
                else "Suboptimal repeats (n=339 correctable failures)"
            )
        ax.barh(y, value, height=0.72, color=color, label=legend_label)
        ax.text(value + 1.2, y, f"{value:.1f}", va="center", fontsize=8.5)
        y_ticks.append(y)
        y_labels.append(label)

    ax.set_yticks(y_ticks)
    ax.set_yticklabels(y_labels, fontsize=8.5)
    ax.set_xlim(0, 68)
    ax.set_xlabel("Share of rounds (%)")
    ax.set_title("A. Stubborn anchoring", fontsize=10, loc="left")
    ax.legend(
        frameon=False,
        loc="center right",
        bbox_to_anchor=(1.0, 0.63),
        fontsize=7.5,
        borderaxespad=0,
    )
    polish_axis(ax, grid_axis="x")


def draw_deference_panel(ax) -> None:
    bar_width = 0.36
    positions = range(len(DEFERENCE_GROUPS))

    for pos, (label, suboptimal, optimal) in zip(positions, DEFERENCE_GROUPS):
        ax.bar(pos - bar_width / 2, suboptimal, bar_width, color=RED, label="Suboptimal" if pos == 0 else None)
        ax.bar(pos + bar_width / 2, optimal, bar_width, color=BLUE, label="Optimal" if pos == 0 else None)
        ax.text(pos - bar_width / 2, suboptimal + 0.5, f"{suboptimal:.1f}", ha="center", fontsize=8.5)
        ax.text(pos + bar_width / 2, optimal + 0.5, f"{optimal:.1f}", ha="center", fontsize=8.5)

    ax.set_xticks(list(positions))
    ax.set_xticklabels([g[0] for g in DEFERENCE_GROUPS], fontsize=8.5)
    ax.set_ylim(0, 26)
    ax.set_ylabel("Share of deferences (%)")
    ax.set_title("B. First-proposal deference outcomes", fontsize=10, loc="left")
    ax.legend(frameon=False, loc="upper left", fontsize=8.5)
    polish_axis(ax, grid_axis="y")


def draw_compliance_panel(ax) -> None:
    positions = range(len(COMPLIANCE_ROWS))
    values = [v for _, v in COMPLIANCE_ROWS]

    ax.bar(positions, values, width=0.55, color=BLUE)
    for pos, value in zip(positions, values):
        ax.text(pos, value + 0.8, f"{value:.0f}", ha="center", fontsize=8.5)

    ax.set_xticks(list(positions))
    ax.set_xticklabels([m for m, _ in COMPLIANCE_ROWS], fontsize=8.5)
    ax.set_ylim(0, 52)
    ax.set_ylabel("Proposals accepted (%)")
    ax.set_title("C. Proposal compliance by model", fontsize=10, loc="left")
    polish_axis(ax, grid_axis="y")


def plot_figure(fig_dir: Path) -> Path:
    fig, axes = plt.subplots(
        1,
        3,
        figsize=(12.5, 3.9),
        gridspec_kw={"width_ratios": [1.45, 1.0, 0.85]},
    )
    draw_anchoring_panel(axes[0])
    draw_deference_panel(axes[1])
    draw_compliance_panel(axes[2])
    fig.tight_layout()

    path = fig_dir / "anchoring_deference_failures.png"
    fig.savefig(path, dpi=220)
    fig.savefig(path.with_suffix(".pdf"))
    plt.close(fig)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot anchoring and deference failure modes.")
    parser.add_argument("--fig-dir", type=Path, default=DEFAULT_FIG_DIR)
    args = parser.parse_args()

    args.fig_dir.mkdir(parents=True, exist_ok=True)
    print(plot_figure(args.fig_dir))


if __name__ == "__main__":
    main()
