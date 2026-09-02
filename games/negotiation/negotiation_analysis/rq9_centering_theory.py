"""RQ9: Centering Theory Analysis.

Operationalise Centering Theory (MT-PingEval) to compute coherence scores.
Compare stable vs shifting and collaborative vs competitive conditions.

Usage:
    uv run python scripts/analysis/rq9_centering_theory.py
"""

import re
import argparse
from collections import Counter

import numpy as np
import pandas as pd
import spacy
from scipy import stats

from negotiation_analysis.data_loader import add_common_args, load_games_from_args, load_experiment_data

nlp = spacy.load("en_core_web_sm")


def normalize_np(text: str) -> str:
    """Normalise a noun phrase: lowercase, strip punctuation, collapse whitespace."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_centers(doc) -> tuple[list[str], str | None]:
    """Extract forward-looking centers (Cf) and preferred center (Cp).

    Returns (cf list in salience order, preferred center or None).
    """
    subjects: list[str] = []
    objects: list[str] = []
    other: list[str] = []

    for chunk in doc.noun_chunks:
        np_text = normalize_np(chunk.text)
        if not np_text:
            continue
        head_dep = chunk.root.dep_
        if head_dep in ("nsubj", "nsubjpass"):
            if np_text not in subjects:
                subjects.append(np_text)
        elif head_dep in ("dobj", "iobj", "pobj"):
            if np_text not in objects:
                objects.append(np_text)
        else:
            if np_text not in other:
                other.append(np_text)

    cf = subjects + objects + other
    cp = cf[0] if cf else None
    return cf, cp


def compute_backward_center(
    prev_cf: list[str], curr_cf: list[str]
) -> str | None:
    """Backward-looking center: highest-ranked prev_cf entity in curr_cf."""
    curr_set = set(curr_cf)
    for entity in prev_cf:
        if entity in curr_set:
            return entity
    return None


def classify_transition(
    prev_cb: str | None, curr_cb: str | None, curr_cp: str | None
) -> str:
    """Classify centering transition type."""
    if curr_cb is None:
        return "rough_shift"

    same_cb = (curr_cb == prev_cb) if prev_cb is not None else False
    cb_is_cp = curr_cb == curr_cp

    if same_cb and cb_is_cp:
        return "continue"
    elif same_cb and not cb_is_cp:
        return "retain"
    elif not same_cb and cb_is_cp:
        return "smooth_shift"
    else:
        return "rough_shift"


def compute_centering_score(transitions: list[str]) -> float:
    """Compute centering coherence score (0–3 scale)."""
    if not transitions:
        return np.nan
    counts = Counter(transitions)
    total = len(transitions)
    c = counts.get("continue", 0) / total
    r = counts.get("retain", 0) / total
    ss = counts.get("smooth_shift", 0) / total
    return 3 * c + 2 * r + 1 * ss


def analyze_round_centering(messages: list[dict]) -> dict:
    """Compute centering metrics for one round's cheap talk."""
    public_msgs = [m for m in messages if m.get("type") != "thinking"]
    if len(public_msgs) < 2:
        return {
            "centering_score": np.nan,
            "transitions": [],
            "n_utterances": len(public_msgs),
            "transition_counts": {},
        }

    utterances = []
    for m in public_msgs:
        doc = nlp(str(m.get("message", "")))
        cf, cp = extract_centers(doc)
        utterances.append({"cf": cf, "cp": cp})

    transitions = []
    prev_cb = None
    for i in range(1, len(utterances)):
        curr_cb = compute_backward_center(
            utterances[i - 1]["cf"], utterances[i]["cf"]
        )
        transition = classify_transition(
            prev_cb, curr_cb, utterances[i]["cp"]
        )
        transitions.append(transition)
        prev_cb = curr_cb

    return {
        "centering_score": compute_centering_score(transitions),
        "transitions": transitions,
        "n_utterances": len(utterances),
        "transition_counts": dict(Counter(transitions)),
    }


def analyze_centering(raw_games: list[dict]) -> dict:
    """Run centering analysis across all games."""
    records = []
    total = len(raw_games)

    for idx, g in enumerate(raw_games):
        if (idx + 1) % 100 == 0:
            print(f"  Processing game {idx + 1}/{total}...")

        for r in g["rounds"]:
            ct = r.get("cheap_talk_transcript", [])
            result = analyze_round_centering(ct)
            tc = result.get("transition_counts", {})

            records.append({
                "game_id": g["game_id"],
                "round_number": r["round_number"],
                "mode": g["mode"],
                "goal_type": g["goal_type"],
                "condition": g["condition"],
                "centering_score": result["centering_score"],
                "n_utterances": result["n_utterances"],
                "n_transitions": len(result["transitions"]),
                "continue_count": tc.get("continue", 0),
                "retain_count": tc.get("retain", 0),
                "smooth_shift_count": tc.get("smooth_shift", 0),
                "rough_shift_count": tc.get("rough_shift", 0),
                "overdrawn": r.get("overdrawn", False),
            })

    centering_df = pd.DataFrame(records)
    valid = centering_df[centering_df["centering_score"].notna()].copy()

    # Statistical tests
    test_results = {}

    # Stable vs shifting
    stable = valid[valid["mode"] == "stable"]["centering_score"]
    shifting = valid[valid["mode"] == "shifting"]["centering_score"]
    if len(shifting) > 1:
        u, p = stats.mannwhitneyu(stable, shifting, alternative="two-sided")
        test_results["stable_vs_shifting"] = {
            "U": u, "p": p,
            "stable_mean": stable.mean(),
            "shifting_mean": shifting.mean(),
        }

    # Collaborative vs competitive
    collab = valid[valid["goal_type"] == "collaborative"]["centering_score"]
    comp = valid[valid["goal_type"] == "competitive"]["centering_score"]
    if len(comp) > 1:
        u, p = stats.mannwhitneyu(collab, comp, alternative="two-sided")
        test_results["collab_vs_comp"] = {
            "U": u, "p": p,
            "collab_mean": collab.mean(),
            "comp_mean": comp.mean(),
        }

    # Centering ~ round correlation (stable)
    stable_valid = valid[valid["mode"] == "stable"]
    if stable_valid["round_number"].nunique() > 1:
        r, p = stats.spearmanr(
            stable_valid["round_number"], stable_valid["centering_score"]
        )
        test_results["stable_round_corr"] = {"r": r, "p": p}

    # Centering vs overdraw
    od = valid[valid["overdrawn"] == True]["centering_score"]
    nod = valid[valid["overdrawn"] == False]["centering_score"]
    if len(od) > 1:
        u, p = stats.mannwhitneyu(nod, od, alternative="two-sided")
        test_results["od_vs_nod"] = {
            "U": u, "p": p,
            "od_mean": od.mean(),
            "nod_mean": nod.mean(),
        }

    return {
        "centering_df": centering_df,
        "valid": valid,
        "test_results": test_results,
    }


def print_summary(results: dict) -> None:
    print("=" * 60)
    print("RQ9: Centering Theory Analysis")
    print("=" * 60)

    valid = results["valid"]
    print(f"\nValid rounds: {len(valid)} (with >= 2 utterances)")
    print(f"Mean centering score: {valid['centering_score'].mean():.3f}")

    print("\nBy condition:")
    print(
        valid.groupby("condition")["centering_score"]
        .describe()
        .round(3)
    )

    print("\nTransition type totals:")
    trans_cols = [
        "continue_count", "retain_count",
        "smooth_shift_count", "rough_shift_count",
    ]
    totals = valid[trans_cols].sum()
    total_all = totals.sum()
    for col in trans_cols:
        name = col.replace("_count", "")
        print(f"  {name}: {totals[col]:.0f} ({totals[col]/total_all:.1%})")

    print("\nStatistical tests:")
    for name, res in results["test_results"].items():
        print(f"  {name}: {res}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RQ9: Centering Theory Coherence")
    add_common_args(parser)
    args = parser.parse_args()
    raw_games = load_games_from_args(args)
    print(f'Analyzing centering for {len(raw_games)} games...')
    results = analyze_centering(raw_games)
    print_summary(results)