"""Build a fixed exploratory environment sample outside the main 720-environment cohort.

The output format matches calibration_rounds_v3.json so the same human and LLM
labeling tools can consume it. Each selected environment contributes all rounds.

Usage:
    uv run python -m judge.sample_exploratory_games --n-games 10
"""

import argparse
import json
from pathlib import Path

from negotiation_game.backend.defaults import REPO_ROOT
from negotiation_analysis.data_loader import (
    MAIN_COHORT_RUN_IDS,
    TOMBSTONED_EPISODE_UID_PREFIXES,
    load_experiment_data,
)

DEFAULT_EXTRACTED_DIR = REPO_ROOT / "judge" / "output" / "extracted"
DEFAULT_OUTPUT = REPO_ROOT / "judge" / "output" / "exploratory_10games_v3.json"


def is_outside_main_cohort(environment: dict) -> bool:
    run_id = environment.get("episode_id")
    episode_uid = environment.get("episode_uid", "")
    return run_id not in MAIN_COHORT_RUN_IDS and episode_uid not in TOMBSTONED_EPISODE_UID_PREFIXES


def has_agent_talk(extracted_game: dict) -> bool:
    for rnd in extracted_game.get("rounds", []):
        for turn in rnd.get("cheap_talk", []):
            if turn.get("speaker") in ("agent_a", "agent_b") and (turn.get("speech") or turn.get("thinking")):
                return True
    return False


def load_extracted_game(extracted_dir: Path, episode_uid: str) -> dict | None:
    path = extracted_dir / f"{episode_uid}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def round_payloads_for_game(environment: dict, extracted_game: dict) -> list[dict]:
    rows: list[dict] = []
    for rnd in extracted_game.get("rounds", []):
        rows.append({
            "episode_uid": extracted_game["episode_uid"],
            "round_number": int(rnd["round_number"]),
            "model_a": extracted_game.get("model_a"),
            "model_b": extracted_game.get("model_b"),
            "mode": extracted_game.get("mode"),
            "shifting_agent": extracted_game.get("shifting_agent"),
            "mc_ratio": extracted_game.get("mc_ratio"),
            "experiment_label": environment.get("experiment_label"),
            "episode_id": environment.get("episode_id"),
            "round": rnd,
        })
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Sample exploratory games outside the main cohort.")
    parser.add_argument("--n-games", type=int, default=10)
    parser.add_argument("--extracted-dir", type=Path, default=DEFAULT_EXTRACTED_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    games = load_experiment_data()
    candidates: list[tuple[dict, dict]] = []
    for environment in sorted(games, key=lambda g: g.get("episode_uid", "")):
        if not is_outside_main_cohort(environment):
            continue
        if environment.get("schema_version", 0) < 5:
            continue
        extracted_game = load_extracted_game(args.extracted_dir, environment["episode_uid"])
        if extracted_game is None:
            continue
        if not has_agent_talk(extracted_game):
            continue
        candidates.append((environment, extracted_game))

    selected = candidates[:args.n_games]
    if len(selected) < args.n_games:
        raise RuntimeError(f"Only found {len(selected)} eligible games; requested {args.n_games}.")

    rounds: list[dict] = []
    selected_games: list[dict] = []
    for environment, extracted_game in selected:
        selected_games.append({
            "episode_uid": environment["episode_uid"],
            "experiment_label": environment.get("experiment_label"),
            "episode_id": environment.get("episode_id"),
            "schema_version": environment.get("schema_version"),
            "num_rounds": len(extracted_game.get("rounds", [])),
        })
        rounds.extend(round_payloads_for_game(environment, extracted_game))

    payload = {
        "version": 1,
        "selection_name": "exploratory_10games_outside_main_cohort_v3",
        "selection_criteria": {
            "excluded_episode_ids": sorted(MAIN_COHORT_RUN_IDS),
            "excluded_tombstoned_episode_uids": sorted(TOMBSTONED_EPISODE_UID_PREFIXES),
            "min_schema_version": 5,
            "requires_extracted_context": True,
            "requires_agent_talk": True,
            "ordering": "lexicographic episode_uid, first 10 eligible games",
        },
        "games": selected_games,
        "rounds": rounds,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(payload, f, indent=2)

    outcome_counts: dict[str, int] = {}
    for row in rounds:
        outcome = row["round"].get("round_outcome", "unknown")
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1

    print(f"Wrote {args.output}")
    print(f"Games: {len(selected_games)}")
    print(f"Rounds: {len(rounds)}")
    print("Outcomes:", ", ".join(f"{k}={v}" for k, v in sorted(outcome_counts.items())))
    print("Environment IDs:", ", ".join(g["episode_uid"] for g in selected_games))


if __name__ == "__main__":
    main()
