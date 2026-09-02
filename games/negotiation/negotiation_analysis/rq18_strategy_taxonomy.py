"""RQ18: Strategy Taxonomy — classify negotiation strategies without LLM-as-judge.

Metrics:
    1. Sharing (self-proposal): rate of rounds where first proposer shares own intended allocation
    2. Proposal (other-proposal): rate of rounds where first proposer suggests opponent's allocation
    3. Fairness appeal: regex-based detection of fairness-related language
    4. Threat: regex-based detection of threatening language
    5. Turn-taking: reward alternation between agents across consecutive rounds (stable games only)
    6. Win-stay/lose-shift: allocation persistence conditioned on overdraw outcome

Usage:
    uv run python -m scripts.analysis.rq18_strategy_taxonomy
"""

import re
import argparse

import numpy as np
import pandas as pd

from negotiation_analysis.data_loader import add_common_args, load_dataset, load_dataset_from_args
from negotiation_analysis.models import NegotiationDataset
from negotiation_analysis.rq12_first_proposal_deference import (
    analyze_first_proposal_deference,
    extract_all_proposals,
)
from negotiation_analysis.rq13_anchoring import analyze_anchoring

FAIRNESS_PATTERN = re.compile(
    r"\b(?:fair(?:ly|ness)?|equal(?:ly)?|split|half|50[/-]50|even(?:ly)?|"
    r"both benefit|mutual(?:ly)?|equitabl[ey]|compromise|balanced?)\b",
    re.IGNORECASE,
)

THREAT_PATTERN = re.compile(
    r"\b(?:waste|punish|nothing|zero|take everything|all of it|"
    r"overdraw|annul|block(?:ing)?|refuse|won'?t cooperate|consequences|"
    r"penalize|retaliat|hurt)\b",
    re.IGNORECASE,
)


def _scan_transcript(rounds: list) -> list[dict]:
    """Scan each round's transcript for fairness appeals and threats.

    Returns per-round records with both aggregate and per-speaker flags.
    Accepts either NegotiationRound objects or legacy raw round dicts.
    """
    records = []
    for r in rounds:
        if hasattr(r, "events"):
            speech_events = [e for e in r.events if e.type == "speech" and e.speaker != "system"]
            by_speaker: dict[str, list[str]] = {}
            for e in speech_events:
                by_speaker.setdefault(e.speaker, []).append(e.content)
            round_number = r.round_number
        else:
            transcript = r.get("cheap_talk_transcript", [])
            speech = [e for e in transcript if e.get("type") != "thinking" and e.get("speaker") != "system"]
            by_speaker = {}
            for e in speech:
                by_speaker.setdefault(e.get("speaker", ""), []).append(e.get("message", ""))
            round_number = r["round_number"]

        all_text = " ".join(t for texts in by_speaker.values() for t in texts)
        fairness_matches = FAIRNESS_PATTERN.findall(all_text)
        threat_matches = THREAT_PATTERN.findall(all_text)

        rec = {
            "round_number": round_number,
            "has_fairness_appeal": len(fairness_matches) > 0,
            "fairness_count": len(fairness_matches),
            "has_threat": len(threat_matches) > 0,
            "threat_count": len(threat_matches),
        }
        for agent in ("agent_a", "agent_b"):
            agent_text = " ".join(by_speaker.get(agent, []))
            rec[f"{agent}_has_fairness"] = bool(FAIRNESS_PATTERN.search(agent_text))
            rec[f"{agent}_has_threat"] = bool(THREAT_PATTERN.search(agent_text))
        records.append(rec)
    return records


def _compute_turn_taking(games: list) -> pd.DataFrame:
    """Compute reward alternation for stable (non-rotating) games.

    turn_taking_2: fraction of consecutive round pairs where the higher-reward
    agent flips (A>B then B>A or vice versa).
    turn_taking_4: fraction of 4-round windows with full alternation (ABAB or BABA).

    Accepts either NegotiationGame objects or legacy raw game dicts.
    """
    records = []
    for g in games:
        if hasattr(g, "is_rotating"):
            # NegotiationGame object
            if g.is_rotating:
                continue
            game_id = g.game_id
            rounds = sorted(g.rounds, key=lambda r: r.round_number)
            if len(rounds) < 2:
                continue
            a_wins = [r.agent_a_reward > r.agent_b_reward for r in rounds]
        else:
            # Legacy raw dict
            per_round = g.get("per_round_scenarios")
            if per_round is not None and len(per_round) > 1:
                continue
            game_id = g["game_id"]
            rounds = sorted(g["rounds"], key=lambda x: x["round_number"])
            if len(rounds) < 2:
                continue
            a_wins = [r.get("agent_a_reward", 0) > r.get("agent_b_reward", 0) for r in rounds]

        # 2-round alternation
        flips_2 = sum(1 for i in range(1, len(a_wins)) if a_wins[i] != a_wins[i - 1])
        pairs_2 = len(a_wins) - 1

        # 4-round alternation
        flips_4 = 0
        windows_4 = 0
        for i in range(len(a_wins) - 3):
            windows_4 += 1
            window = a_wins[i:i + 4]
            if all(window[j] != window[j + 1] for j in range(3)):
                flips_4 += 1

        records.append({
            "game_id": game_id,
            "turn_taking_rate_2": flips_2 / pairs_2 if pairs_2 > 0 else np.nan,
            "turn_taking_rate_4": flips_4 / windows_4 if windows_4 > 0 else np.nan,
            "n_rounds": len(rounds),
        })

    return pd.DataFrame(records)


def _compute_win_stay_lose_shift(anch_df: pd.DataFrame) -> pd.DataFrame:
    """From anchoring data, compute win-stay and lose-shift rates per game.

    Win (stay) = previous round was not overdrawn (aligns with anchoring analysis).
    Lose (shift) = previous round was not optimal (overdrawn OR suboptimal).
    Stay = same joint allocation as previous round.
    Shift = different joint allocation from previous round.
    """
    if anch_df.empty:
        return pd.DataFrame(columns=["game_id", "win_stay_rate", "lose_shift_rate"])

    records = []
    for game_id, gdf in anch_df.groupby("game_id"):
        prev_overdrawn = gdf["prev_overdrawn"].fillna(True).astype(bool)
        prev_optimal = gdf["prev_joint_optimal"].fillna(False).astype(bool)
        win_rows = gdf[~prev_overdrawn]
        lose_rows = gdf[~prev_optimal]

        win_stay = win_rows["alloc_same_as_prev"].mean() if len(win_rows) > 0 else np.nan
        lose_shift = 1.0 - lose_rows["alloc_same_as_prev"].mean() if len(lose_rows) > 0 else np.nan

        records.append({
            "game_id": game_id,
            "win_stay_rate": win_stay,
            "lose_shift_rate": lose_shift,
        })

    return pd.DataFrame(records)


def analyze_strategy_taxonomy(dataset: NegotiationDataset | None = None) -> dict:
    """Classify negotiation strategies across V5+ games.

    Returns dict with:
        - round_df: per-round taxonomy flags (fairness, threat)
        - game_df: per-game aggregated strategy rates
        - summary: overall means
    """
    if dataset is None:
        dataset = load_dataset()

    v5_dataset = NegotiationDataset([g for g in dataset.games if g.schema_version >= 5])
    if not v5_dataset.games:
        return {"round_df": pd.DataFrame(), "game_df": pd.DataFrame(), "summary": {}}

    v5_games = v5_dataset.games

    # --- 1: Numeric sharing rate (first-person framed, numeric quantities) ---
    sharing_records = []
    for g in v5_games:
        resource_types = g.config.get("resource_types", ["wood", "stone", "gold"])
        for r in g.rounds:
            ct = r.cheap_talk_transcript
            props = extract_all_proposals(ct, resource_types)
            sharing_records.append({
                "game_id": g.game_id,
                "round_number": r.round_number,
                "a_has_numeric_proposal": props["agent_a"]["has_numeric_proposal"],
                "b_has_numeric_proposal": props["agent_b"]["has_numeric_proposal"],
                "either_has_numeric_proposal": (
                    props["agent_a"]["has_numeric_proposal"]
                    or props["agent_b"]["has_numeric_proposal"]
                ),
            })
    sharing_round_df = pd.DataFrame(sharing_records)

    if not sharing_round_df.empty:
        sharing_by_game = (
            sharing_round_df.groupby("game_id")["either_has_numeric_proposal"]
            .mean()
            .rename("sharing_rate")
        )
    else:
        sharing_by_game = pd.Series(dtype=float, name="sharing_rate")

    # --- 2: Other-proposal rate from rq12 (uses all rounds as denominator) ---
    fpd_results = analyze_first_proposal_deference(v5_dataset)
    fpd_df = fpd_results.get("round_df", pd.DataFrame())
    total_rounds_by_game = {g.game_id: g.num_rounds for g in v5_games}

    if not fpd_df.empty:
        other_counts = fpd_df.groupby("game_id")["has_other_proposal"].sum()
        proposal_by_game = (other_counts / other_counts.index.map(total_rounds_by_game)).rename("proposal_rate")
    else:
        proposal_by_game = pd.Series(dtype=float, name="proposal_rate")

    # --- 3 & 4: Fairness and Threat from transcript regex ---
    round_records = []
    for g in v5_games:
        scan = _scan_transcript(g.rounds)
        for rec in scan:
            rec["game_id"] = g.game_id
        round_records.extend(scan)

    round_df = pd.DataFrame(round_records)

    if not round_df.empty:
        fairness_by_game = round_df.groupby("game_id")["has_fairness_appeal"].mean().rename("fairness_appeal_rate")
        threat_by_game = round_df.groupby("game_id")["has_threat"].mean().rename("threat_rate")
    else:
        fairness_by_game = pd.Series(dtype=float, name="fairness_appeal_rate")
        threat_by_game = pd.Series(dtype=float, name="threat_rate")

    # --- 5: Turn-taking ---
    tt_df = _compute_turn_taking(v5_games)

    # --- 6: Win-stay/lose-shift from rq13 ---
    anch_df = analyze_anchoring(v5_dataset)
    wsls_df = _compute_win_stay_lose_shift(anch_df)

    # --- Assemble game_df ---
    game_ids = [g.game_id for g in v5_games]
    game_df = pd.DataFrame({"game_id": game_ids})

    for series in [sharing_by_game, proposal_by_game, fairness_by_game, threat_by_game]:
        game_df = game_df.merge(series, on="game_id", how="left")

    if not tt_df.empty:
        game_df = game_df.merge(tt_df[["game_id", "turn_taking_rate_2", "turn_taking_rate_4"]], on="game_id", how="left")
    else:
        game_df["turn_taking_rate_2"] = np.nan
        game_df["turn_taking_rate_4"] = np.nan

    if not wsls_df.empty:
        game_df = game_df.merge(wsls_df, on="game_id", how="left")
    else:
        game_df["win_stay_rate"] = np.nan
        game_df["lose_shift_rate"] = np.nan

    summary = {
        "n_games": len(game_df),
        "mean_sharing_rate": game_df["sharing_rate"].mean(),
        "mean_proposal_rate": game_df["proposal_rate"].mean(),
        "mean_fairness_appeal_rate": game_df["fairness_appeal_rate"].mean(),
        "mean_threat_rate": game_df["threat_rate"].mean(),
        "mean_turn_taking_2": game_df["turn_taking_rate_2"].mean(),
        "mean_turn_taking_4": game_df["turn_taking_rate_4"].mean(),
        "mean_win_stay_rate": game_df["win_stay_rate"].mean(),
        "mean_lose_shift_rate": game_df["lose_shift_rate"].mean(),
    }

    # --- Agent-level attribution (for per-model breakdowns) ---
    agent_records = []
    for g in v5_games:
        resource_types = g.config.get("resource_types", ["wood", "stone", "gold"])
        scan = _scan_transcript(g.rounds)
        for r, sc in zip(g.rounds, scan):
            ct = r.cheap_talk_transcript
            props = extract_all_proposals(ct, resource_types)
            for agent, model in [("agent_a", g.model_a), ("agent_b", g.model_b)]:
                agent_records.append({
                    "game_id": g.game_id,
                    "round_number": r.round_number,
                    "model": model,
                    "mc_bucket": g.metadata["mc_bucket"],
                    "has_self_proposal": props[agent]["has_numeric_proposal"],
                    "has_fairness": sc[f"{agent}_has_fairness"],
                    "has_threat": sc[f"{agent}_has_threat"],
                })
    agent_df = pd.DataFrame(agent_records)

    # Add per-agent other-proposal from rq12 (attributed to first_proposer)
    if not fpd_df.empty and "first_proposer_model" in fpd_df.columns:
        other_agent = fpd_df[fpd_df["has_other_proposal"]][
            ["game_id", "round_number", "first_proposer_model"]
        ].rename(columns={"first_proposer_model": "model"})
        other_agent["has_other_proposal"] = True
        agent_df = agent_df.merge(
            other_agent, on=["game_id", "round_number", "model"], how="left",
        )
        agent_df["has_other_proposal"] = agent_df["has_other_proposal"].fillna(False)
    else:
        agent_df["has_other_proposal"] = False

    return {
        "round_df": round_df,
        "game_df": game_df,
        "agent_df": agent_df,
        "summary": summary,
    }


def print_summary(results: dict) -> None:
    print("=" * 60)
    print("RQ18: Strategy Taxonomy")
    print("=" * 60)

    s = results["summary"]
    if not s:
        print("No data.")
        return

    print(f"\nGames analyzed: {s['n_games']}")
    print(f"\n{'Metric':<35} {'Value':>10}")
    print("-" * 47)

    metrics = [
        ("Sharing (self-proposal) rate", "mean_sharing_rate"),
        ("Proposal (other-proposal) rate", "mean_proposal_rate"),
        ("Fairness appeal rate", "mean_fairness_appeal_rate"),
        ("Threat rate", "mean_threat_rate"),
        ("Turn-taking (2-round)", "mean_turn_taking_2"),
        ("Turn-taking (4-round)", "mean_turn_taking_4"),
        ("Win-stay rate", "mean_win_stay_rate"),
        ("Lose-shift rate", "mean_lose_shift_rate"),
    ]
    for label, key in metrics:
        val = s.get(key, np.nan)
        if val is not None and not np.isnan(val):
            print(f"{label:<35} {val:>10.3f}")
        else:
            print(f"{label:<35} {'N/A':>10}")


def print_agent_table(results: dict) -> None:
    """Print per-model × M/C strategy rates at agent-round level."""
    agent_df = results.get("agent_df", pd.DataFrame())
    if agent_df.empty:
        return

    cols = ["has_other_proposal", "has_fairness", "has_threat"]
    labels = ["Proposal", "Fairness", "Threat"]

    print("\n" + "=" * 60)
    print("Agent-level strategy rates (per-model × M/C)")
    print("=" * 60)

    for col, label in zip(cols, labels):
        print(f"\n--- {label} ---")
        tbl = agent_df.pivot_table(
            index="model", columns="mc_bucket", values=col,
            aggfunc="mean", margins=True,
        )
        print(tbl.round(3).to_string())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RQ18: Strategy Taxonomy")
    add_common_args(parser)
    args = parser.parse_args()
    dataset = load_dataset_from_args(args)
    v5_count = sum(1 for g in dataset.games if g.schema_version >= 5)
    print(f"V5+ games: {v5_count}")
    results = analyze_strategy_taxonomy(dataset)
    print_summary(results)
    print_agent_table(results)