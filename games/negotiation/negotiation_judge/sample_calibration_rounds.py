"""Build a small calibration subset for human/LLM rubric agreement.

The output is a JSON file with one item per round. It is intentionally simple
so both the browser labeler and the LLM labeling script can consume the same
round set.

Usage:
    uv run python -m judge.sample_calibration_rounds --n-rounds 18
"""

import argparse
import csv
import json
import random
from pathlib import Path

from negotiation_game.backend.defaults import REPO_ROOT

DEFAULT_EXTRACTED_DIR = REPO_ROOT / "judge" / "output" / "extracted"
DEFAULT_JUDGMENTS_CSV = REPO_ROOT / "judge" / "output" / "judgments.csv"
DEFAULT_OUTPUT = REPO_ROOT / "judge" / "output" / "calibration_rounds_v3.json"
OUTCOME_ORDER = ["overdrawn", "suboptimal", "optimal"]


def load_judged_rounds(judgments_csv: Path) -> list[dict]:
    rounds: list[dict] = []
    with open(judgments_csv) as f:
        for row in csv.DictReader(f):
            for rnd in json.loads(row["rounds_summary"]):
                rounds.append({
                    "game_id": row["game_id"],
                    "round_number": int(rnd["round_number"]),
                    "round_outcome": rnd.get("round_outcome"),
                    "joint_efficiency": rnd.get("joint_efficiency"),
                    "model_a": row.get("model_a"),
                    "model_b": row.get("model_b"),
                    "mode": row.get("mode"),
                    "mc_ratio": row.get("mc_ratio"),
                })
    return rounds


def round_robin_sample(rounds: list[dict], n_rounds: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    buckets = {outcome: [] for outcome in OUTCOME_ORDER}
    buckets["other"] = []

    for row in rounds:
        buckets.get(row.get("round_outcome"), buckets["other"]).append(row)

    for rows in buckets.values():
        rng.shuffle(rows)

    selected: list[dict] = []
    while len(selected) < n_rounds:
        added = False
        for outcome in OUTCOME_ORDER + ["other"]:
            if buckets[outcome] and len(selected) < n_rounds:
                selected.append(buckets[outcome].pop())
                added = True
        if not added:
            break

    selected.sort(key=lambda r: (r["game_id"], r["round_number"]))
    return selected


def attach_round_payloads(selected: list[dict], extracted_dir: Path) -> list[dict]:
    by_game: dict[str, dict] = {}
    payloads: list[dict] = []

    for item in selected:
        gid = item["game_id"]
        if gid not in by_game:
            path = extracted_dir / f"{gid}.json"
            with open(path) as f:
                by_game[gid] = json.load(f)

        game = by_game[gid]
        rnd = next(
            r for r in game["rounds"]
            if int(r["round_number"]) == int(item["round_number"])
        )
        payloads.append({
            "game_id": gid,
            "round_number": int(rnd["round_number"]),
            "model_a": game.get("model_a"),
            "model_b": game.get("model_b"),
            "mode": game.get("mode"),
            "shifting_agent": game.get("shifting_agent"),
            "mc_ratio": game.get("mc_ratio"),
            "round": rnd,
        })

    return payloads


def main() -> None:
    parser = argparse.ArgumentParser(description="Sample rounds for rubric calibration.")
    parser.add_argument("--n-rounds", type=int, default=18)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--judgments-csv", type=Path, default=DEFAULT_JUDGMENTS_CSV)
    parser.add_argument("--extracted-dir", type=Path, default=DEFAULT_EXTRACTED_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    rounds = load_judged_rounds(args.judgments_csv)
    selected = round_robin_sample(rounds, args.n_rounds, args.seed)
    payloads = attach_round_payloads(selected, args.extracted_dir)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump({"version": 1, "rounds": payloads}, f, indent=2)

    counts: dict[str, int] = {}
    for item in payloads:
        outcome = item["round"].get("round_outcome", "unknown")
        counts[outcome] = counts.get(outcome, 0) + 1

    print(f"Wrote {args.output}")
    print(f"Rounds: {len(payloads)}")
    print("Outcome mix:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
