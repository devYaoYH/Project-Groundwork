"""RQ15: Speech Information Density.

Measures the ratio of content words to total words per utterance, filtering
out stopwords/fillers. Analyzed per model and per turn within a round to see
whether agents become more or less informationally dense over time.

Usage:
    uv run python -m scripts.analysis.rq15_information_density
"""

import re
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from negotiation_analysis.data_loader import add_common_args, load_games_from_args, get_agent_models, load_experiment_data

_STOPWORDS_FILE = Path(__file__).parent / "stopwords.txt"
_STOPWORDS: set[str] | None = None


def _load_stopwords() -> set[str]:
    global _STOPWORDS
    if _STOPWORDS is None:
        _STOPWORDS = {
            line.strip().lower()
            for line in _STOPWORDS_FILE.read_text().splitlines()
            if line.strip()
        }
    return _STOPWORDS


def _tokenize(text: str) -> list[str]:
    """Simple whitespace + punctuation tokenizer, lowercase."""
    return re.findall(r"[a-z]+(?:'[a-z]+)?", text.lower())


def compute_information_density(text: str) -> dict:
    """Compute information density for a single utterance.

    Returns dict with total_words, content_words, density.
    """
    stopwords = _load_stopwords()
    tokens = _tokenize(text)
    total = len(tokens)
    if total == 0:
        return {"total_words": 0, "content_words": 0, "density": np.nan}
    content = [t for t in tokens if t not in stopwords]
    return {
        "total_words": total,
        "content_words": len(content),
        "density": len(content) / total,
    }


def analyze_information_density(raw_games: list[dict]) -> dict:
    """Compute per-turn information density across all games.

    Returns dict with:
        - turn_df: one row per utterance with density metrics
        - by_model: mean density per model
        - by_model_turn: density by model x turn_number
        - summary: aggregate stats
    """
    rows = []
    for g in raw_games:
        model_a, model_b = get_agent_models(g)
        model_map = {"agent_a": model_a, "agent_b": model_b}

        for r in g["rounds"]:
            ct = r.get("cheap_talk_transcript", [])
            for entry in ct:
                if entry.get("type") == "thinking":
                    continue
                speaker = entry["speaker"]
                if speaker not in ("agent_a", "agent_b"):
                    continue
                msg = entry.get("message", "")
                info = compute_information_density(msg)
                rows.append({
                    "game_id": g["game_id"],
                    "round_number": r["round_number"],
                    "turn_number": entry.get("turn", 0),
                    "speaker": speaker,
                    "model": model_map[speaker],
                    "total_words": info["total_words"],
                    "content_words": info["content_words"],
                    "density": info["density"],
                })

    turn_df = pd.DataFrame(rows)
    if turn_df.empty:
        return {"turn_df": turn_df, "by_model": pd.DataFrame(),
                "by_model_turn": pd.DataFrame(), "summary": {}}

    # Per-model aggregate
    by_model = (
        turn_df.groupby("model")
        .agg(
            n_turns=pd.NamedAgg(column="density", aggfunc="count"),
            mean_density=pd.NamedAgg(column="density", aggfunc="mean"),
            std_density=pd.NamedAgg(column="density", aggfunc="std"),
            mean_total_words=pd.NamedAgg(column="total_words", aggfunc="mean"),
            mean_content_words=pd.NamedAgg(column="content_words", aggfunc="mean"),
        )
        .round(3)
    )

    # Per model x turn_number (within-round turn position)
    by_model_turn = (
        turn_df.groupby(["model", "turn_number"])
        .agg(
            n=pd.NamedAgg(column="density", aggfunc="count"),
            mean_density=pd.NamedAgg(column="density", aggfunc="mean"),
            sem_density=pd.NamedAgg(column="density", aggfunc="sem"),
        )
        .reset_index()
        .round(3)
    )

    summary = {
        "n_turns": len(turn_df),
        "mean_density": turn_df["density"].mean(),
        "std_density": turn_df["density"].std(),
        "mean_total_words": turn_df["total_words"].mean(),
        "mean_content_words": turn_df["content_words"].mean(),
    }

    return {
        "turn_df": turn_df,
        "by_model": by_model,
        "by_model_turn": by_model_turn,
        "summary": summary,
    }


def print_summary(results: dict) -> None:
    print("=" * 60)
    print("RQ15: Speech Information Density")
    print("=" * 60)

    s = results["summary"]
    print(f"\nTotal turns analyzed: {s['n_turns']}")
    print(f"Mean density (content/total): {s['mean_density']:.3f} ± {s['std_density']:.3f}")
    print(f"Mean words per turn: {s['mean_total_words']:.1f} (content: {s['mean_content_words']:.1f})")

    print("\nBy model:")
    print(results["by_model"].to_string())

    bmt = results["by_model_turn"]
    if not bmt.empty:
        # Show first 6 turns per model
        print("\nDensity by turn position (first 6 turns):")
        pivot = bmt[bmt["turn_number"] < 6].pivot_table(
            index="model", columns="turn_number", values="mean_density",
        )
        if not pivot.empty:
            print(pivot.round(3).to_string())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RQ15: Information Density")
    add_common_args(parser)
    args = parser.parse_args()
    raw_games = load_games_from_args(args)
    results = analyze_information_density(raw_games)
    print_summary(results)