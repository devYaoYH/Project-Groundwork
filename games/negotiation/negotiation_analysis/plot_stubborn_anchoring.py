"""Plot stubborn-anchoring rates as a small grouped bar chart (Section 5.5).

Stubborn anchoring = repeating the exact prior joint allocation in a round
where the previous round was suboptimal but non-overdrawn, i.e. a failure the
dyad could have corrected. Denominators are eligible consecutive-round pairs
in non-rotating games from the 720-environment main cohort, verified against
`uv run python -m scripts.analysis.rq13_anchoring --main-cohort`:
overall 99/339, stable 74/182, shifting 25/157, M/C=0.5 49/173,
M/C=1.0 29/63.

Usage:
    uv run python -m scripts.analysis.plot_stubborn_anchoring
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt

DEFAULT_FIG_DIR = Path("figures")

BLUE = "#2f6f9f"

# label, stubborn count, eligible count, x position
BARS = [
    ("All correctable\nfailures", 99, 339, 0.0),
    ("Stable\ngames", 74, 182, 1.6),
    ("Shifting\ngames", 25, 157, 2.6),
    ("M/C = 0.5", 49, 173, 4.2),
    ("M/C = 1.0", 29, 63, 5.2),
]

GROUP_HEADERS = [
    ("Overall", 0.0),
    ("By mode", 2.1),
    ("By M/C ratio", 4.7),
]

FOOTNOTE = (
    "Share of eligible rounds: suboptimal, non-overdrawn rounds (with the prior round's\n"
    "scenario unchanged) where the dyad repeats the exact same joint allocation."
)


def plot_figure(fig_dir: Path) -> Path:
    fig, ax = plt.subplots(figsize=(6.2, 3.4))

    for label, stubborn, eligible, x in BARS:
        pct = 100.0 * stubborn / eligible
        ax.bar(x, pct, width=0.8, color=BLUE)
        ax.text(x, pct + 1.0, f"{pct:.1f}%", ha="center", fontsize=9, fontweight="bold")
        ax.text(x, pct + 6.0, f"({stubborn}/{eligible})", ha="center", fontsize=7.5, color="#555555")

    for header, x in GROUP_HEADERS:
        ax.text(x, 56, header, ha="center", fontsize=9, color="#333333", style="italic")

    ax.set_xticks([x for _, _, _, x in BARS])
    ax.set_xticklabels([label for label, _, _, _ in BARS], fontsize=8.5)
    ax.set_ylim(0, 60)
    ax.set_ylabel("Stubborn-anchor rate (%)", fontsize=9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.grid(axis="y", color="#e8e8e8", linewidth=0.8)
    ax.set_axisbelow(True)
    fig.text(0.01, 0.01, FOOTNOTE, fontsize=7, color="#555555", va="bottom")
    fig.tight_layout(rect=(0, 0.1, 1, 1))

    path = fig_dir / "stubborn_anchoring_rates.png"
    fig.savefig(path, dpi=220)
    fig.savefig(path.with_suffix(".pdf"))
    plt.close(fig)
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Plot stubborn-anchoring rates.")
    parser.add_argument("--fig-dir", type=Path, default=DEFAULT_FIG_DIR)
    args = parser.parse_args()

    args.fig_dir.mkdir(parents=True, exist_ok=True)
    print(plot_figure(args.fig_dir))


if __name__ == "__main__":
    main()
