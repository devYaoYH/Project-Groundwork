"""Build environment-level samples for taxonomy labeling runs.

Outputs the same JSON shape consumed by judge.run_taxonomy_game_labeling, with
all rounds for each selected environment. The builder reads local cached episodes for
cohort membership and local extracted contexts for transcript payloads.

Usage:
    uv run python -m judge.build_taxonomy_game_sample --cohort main720
    uv run python -m judge.build_taxonomy_game_sample --cohort transparency120
"""

import argparse
import json
from pathlib import Path

from negotiation_game.backend.defaults import REPO_ROOT
from negotiation_analysis.data_loader import (
    CACHE_PATH,
    MAIN_COHORT_RUN_IDS,
    TOMBSTONED_EPISODE_UID_PREFIXES,
    load_from_cache,
)
from negotiation_analysis.jq_transparency_failure_modes import TRANSPARENCY_RUN_IDS

DEFAULT_MAIN_EXTRACTED_DIR = REPO_ROOT / "judge" / "output" / "extracted"
DEFAULT_TRANSPARENCY_EXTRACTED_DIR = REPO_ROOT / "judge" / "output" / "transparency" / "extracted"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "judge" / "output"


def has_agent_talk(extracted_game: dict) -> bool:
    return any(
        turn.get("speaker") in ("agent_a", "agent_b") and (turn.get("speech") or turn.get("thinking"))
        for rnd in extracted_game.get("rounds", [])
        for turn in rnd.get("cheap_talk", [])
    )


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


def select_games(raw_games: list[dict], cohort: str) -> tuple[list[dict], Path, dict]:
    if cohort == "main720":
        selected = [
            environment for environment in raw_games
            if environment.get("episode_id") in MAIN_COHORT_RUN_IDS
            and environment.get("episode_uid") not in TOMBSTONED_EPISODE_UID_PREFIXES
        ]
        criteria = {
            "cohort": "main_720",
            "episode_ids": sorted(MAIN_COHORT_RUN_IDS),
            "excluded_tombstoned_episode_uids": sorted(TOMBSTONED_EPISODE_UID_PREFIXES),
        }
        return selected, DEFAULT_MAIN_EXTRACTED_DIR, criteria

    if cohort == "transparency120":
        selected = [
            environment for environment in raw_games
            if environment.get("episode_id") in TRANSPARENCY_RUN_IDS
            and environment.get("episode_uid") not in TOMBSTONED_EPISODE_UID_PREFIXES
        ]
        criteria = {
            "cohort": "full_transparency_120",
            "episode_ids": sorted(TRANSPARENCY_RUN_IDS),
            "excluded_tombstoned_episode_uids": sorted(TOMBSTONED_EPISODE_UID_PREFIXES),
        }
        return selected, DEFAULT_TRANSPARENCY_EXTRACTED_DIR, criteria

    raise ValueError(f"Unknown cohort: {cohort}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build taxonomy environment sample JSON for a cohort.")
    parser.add_argument("--cohort", choices=["main720", "transparency120"], required=True)
    parser.add_argument("--cache", type=Path, default=CACHE_PATH)
    parser.add_argument("--extracted-dir", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    raw_games = load_from_cache(args.cache)
    selected_raw, default_extracted_dir, criteria = select_games(raw_games, args.cohort)
    extracted_dir = args.extracted_dir or default_extracted_dir
    output = args.output or DEFAULT_OUTPUT_DIR / f"{args.cohort}_taxonomy_v3.json"

    games: list[dict] = []
    rounds: list[dict] = []
    missing: list[str] = []
    no_talk: list[str] = []
    for environment in sorted(selected_raw, key=lambda g: g.get("episode_uid", "")):
        episode_uid = environment["episode_uid"]
        extracted_game = load_extracted_game(extracted_dir, episode_uid)
        if extracted_game is None:
            missing.append(episode_uid)
            continue
        if not has_agent_talk(extracted_game):
            no_talk.append(episode_uid)
            continue
        games.append({
            "episode_uid": episode_uid,
            "experiment_label": environment.get("experiment_label"),
            "episode_id": environment.get("episode_id"),
            "schema_version": environment.get("schema_version"),
            "model_a": extracted_game.get("model_a"),
            "model_b": extracted_game.get("model_b"),
            "mode": extracted_game.get("mode"),
            "shifting_agent": extracted_game.get("shifting_agent"),
            "mc_ratio": extracted_game.get("mc_ratio"),
            "num_rounds": len(extracted_game.get("rounds", [])),
        })
        rounds.extend(round_payloads_for_game(environment, extracted_game))

    payload = {
        "version": 1,
        "selection_name": f"{args.cohort}_taxonomy_v3",
        "selection_criteria": {
            **criteria,
            "requires_extracted_context": True,
            "requires_agent_talk": True,
            "source_cache": str(args.cache),
            "extracted_dir": str(extracted_dir),
        },
        "games": games,
        "rounds": rounds,
        "missing_extracted_episode_uids": missing,
        "no_agent_talk_episode_uids": no_talk,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"Wrote {output}")
    print(f"Games: {len(games)}")
    print(f"Rounds: {len(rounds)}")
    if missing:
        print(f"Missing extracted contexts: {len(missing)}")
    if no_talk:
        print(f"No agent talk: {len(no_talk)}")


if __name__ == "__main__":
    main()
