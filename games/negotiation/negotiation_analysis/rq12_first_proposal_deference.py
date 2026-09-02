"""RQ12: First Proposal Deference & Sycophancy Analysis

Measures whether agents defer to opponents' proposed allocations (sycophancy)
and whether proposers follow through on their own stated plans.

Usage:
    uv run python -m scripts.analysis.rq12_first_proposal_deference
"""

import argparse
import json
import re
from pathlib import Path

import pandas as pd

from negotiation_analysis.data_loader import add_common_args, load_dataset_from_args, RESOURCES
from negotiation_analysis.models import NegotiationDataset
from negotiation_analysis.rq6_stated_vs_actual import extract_intentions


def extract_proposal_for_other(message: str, resource_types: list[str]) -> dict[str, int]:
    """Extract what the speaker suggests the OTHER agent should buy.

    Looks for "you" framing: "you take 3 wood", "you could get 6 stone".
    Also handles compound proposals: "you take 8 wood and 2 stone".

    Args:
        message: The message text to parse
        resource_types: List of resource names for this game (e.g., ["plasma", "crystal"])
    """
    msg = str(message).lower()
    proposals: dict[str, int] = {}

    # Build a quantity pattern for any resource
    _res_pat = "|".join(resource_types)
    _qty_res = r"(\d+)\s*(?:-\d+)?\s*(?:units?\s+(?:of\s+)?)?(" + _res_pat + r")"

    # Strategy: find "you <verb>" clauses and extract all qty+resource pairs
    # from the clause (up to next sentence boundary or "and I" / "I'll" pivot)
    you_verb = (
        r"you\s+(?:could\s+|should\s+|might\s+|can\s+|would\s+)?"
        r"(?:take|get|buy|grab|pick up|go for|focus on|claim|do)"
    )
    for m in re.finditer(you_verb, msg):
        # Extract the clause after "you <verb>" until sentence end or speaker pivot
        start = m.end()
        rest = msg[start:start + 200]
        # Cut at sentence boundaries or speaker pivots
        cut = re.search(r"[.!?]|\bi(?:'ll| will| could| would| can)\b|\bthat\b|\bwhile\b", rest)
        clause = rest[:cut.start()] if cut else rest
        # Extract all qty+resource pairs in this clause
        for qm in re.finditer(_qty_res, clause):
            qty = int(qm.group(1))
            res = qm.group(2)
            if res in resource_types:
                proposals[res] = qty

    # Also catch "leave/give you N resource" patterns
    for r in resource_types:
        if r in proposals:
            continue
        pat = (
            r"(?:leave|give|allocate|assign)\s+(?:you\s+)?"
            r"(\d+)\s*(?:-\d+)?\s*(?:units?\s+(?:of\s+)?)?"
            + r + r"(?:\s+(?:for|to)\s+you)?"
        )
        m = re.search(pat, msg)
        if m:
            proposals[r] = int(m.group(1))

    return proposals


def extract_numeric_self_proposal(message: str, resource_types: list[str]) -> dict[str, int]:
    """Extract numeric self-proposals requiring first-person framing.

    Only matches when the speaker states their *own* intended quantities,
    e.g. "I'll take 5 wood", "I plan to buy 3 gold", "my allocation: wood 6".
    Bare mentions like "there are 10 wood" do NOT match.

    Returns dict mapping resource -> quantity (empty if no first-person numeric plan found).
    """
    msg = str(message).lower()
    proposals: dict[str, int] = {}

    # Pattern 1: first-person verb + quantity + resource
    # "I'll take 5 wood", "I want to buy 3 gold", "I'm going for 6 stone"
    _res_pat = "|".join(re.escape(r.lower()).replace(r"\_", r"[_ ]") for r in resource_types)
    first_person_qty = (
        r"(?:i'?ll|i will|i would|i want to|i plan to|i'm going to|i intend to|"
        r"i'd like to|let me|i'm planning to|i should|i could)\s+"
        r"(?:take|buy|get|grab|claim|pick up|go for|request|allocate|spend on|use)\s+"
        r"(?:about\s+|around\s+|roughly\s+)?"
        r"(\d+)\s*(?:units?\s+(?:of\s+)?)?(" + _res_pat + r")\b"
    )
    for m in re.finditer(first_person_qty, msg):
        qty = int(m.group(1))
        res = m.group(2).replace(" ", "_")
        if res in resource_types:
            proposals[res] = qty

    # Pattern 2: "my <resource>: <qty>" or "my allocation: <resource> <qty>"
    for r in resource_types:
        if r in proposals:
            continue
        r_lower = r.lower()
        r_pattern = re.escape(r_lower).replace(r"\_", r"[_ ]")
        pat = r"my\s+" + r_pattern + r"\s*[:\-=]\s*(\d+)"
        m = re.search(pat, msg)
        if m:
            proposals[r] = int(m.group(1))

    # Pattern 3: "I'll focus on <resource>" + qty in same clause
    # "I'll focus on wood and take 5"
    focus_qty = (
        r"(?:i'?ll|i will|i'm going to)\s+"
        r"(?:focus|concentrate|speciali[sz]e)\s+(?:on|in)\s+"
        r"(?:the\s+)?(" + _res_pat + r")\b"
        r"[^.!?]*?(\d+)"
    )
    for m in re.finditer(focus_qty, msg):
        res = m.group(1).replace(" ", "_")
        qty = int(m.group(2))
        if res in resource_types and res not in proposals:
            proposals[res] = qty

    return proposals


def extract_all_proposals(
    transcript: list[dict], resource_types: list[str],
) -> dict[str, dict]:
    """Extract numeric self-proposals from ALL speech messages for each agent.

    Returns dict with agent_a and agent_b keys, each containing:
        - has_numeric_proposal: bool (any message had a numeric self-proposal)
        - n_messages_with_proposals: int
        - all_proposals: list[dict] (resource->qty for each message that had one)
        - combined: dict (union of all proposals, last value wins)
    """
    public_msgs = [
        e for e in transcript
        if e.get("type") != "thinking" and e.get("speaker") in ("agent_a", "agent_b")
    ]

    result = {}
    for agent in ["agent_a", "agent_b"]:
        agent_msgs = [e for e in public_msgs if e["speaker"] == agent]
        all_proposals = []
        combined: dict[str, int] = {}

        for entry in agent_msgs:
            msg = entry.get("message", "")
            if '"thinking"' in msg[:50]:
                continue
            proposal = extract_numeric_self_proposal(msg, resource_types)
            if proposal:
                all_proposals.append(proposal)
                combined.update(proposal)

        result[agent] = {
            "has_numeric_proposal": len(all_proposals) > 0,
            "n_messages_with_proposals": len(all_proposals),
            "all_proposals": all_proposals,
            "combined": combined,
        }

    return result


def extract_first_proposals(transcript: list[dict], resource_types: list[str]) -> dict:
    """Extract first proposals from each agent in a round's transcript.

    Args:
        transcript: Round's cheap talk transcript
        resource_types: List of resource names for this game

    Returns dict with agent_a/agent_b proposal info and first_proposer.
    """
    public_msgs = [e for e in transcript if e.get("type") != "thinking"]

    result: dict = {"agent_a": None, "agent_b": None, "first_proposer": None}
    first_turn = float("inf")

    for agent in ["agent_a", "agent_b"]:
        for entry in public_msgs:
            if entry["speaker"] != agent:
                continue
            msg = entry.get("message", "")
            # Skip messages that are leaked thinking blocks
            if '"thinking"' in msg[:50]:
                continue
            self_proposal = extract_intentions(msg, resource_types=resource_types)
            other_proposal = extract_proposal_for_other(msg, resource_types)

            if self_proposal or other_proposal:
                turn = entry.get("turn", 0)
                result[agent] = {
                    "self": self_proposal,
                    "for_other": other_proposal,
                    "turn": turn,
                    "message": msg,
                }
                if turn < first_turn:
                    first_turn = turn
                    result["first_proposer"] = agent
                break  # first message with extractable content

    return result


def _resource_match(proposal: dict[str, int], actual: dict) -> tuple[bool, bool]:
    """Compare a proposal to actual allocation.

    Returns (resource_match, qty_match).
    resource_match: proposed resources are a subset of actual (ignoring -1 focus markers).
    qty_match: all proposed quantities match exactly.
    """
    if not proposal:
        return (False, False)

    specific = {r: q for r, q in proposal.items() if q > 0}
    if not specific:
        return (False, False)

    res_match = all(actual.get(r, 0) > 0 for r in specific)
    qty_match = all(actual.get(r, 0) == q for r, q in specific.items())
    return (res_match, qty_match)


def _proposal_joint_efficiency(
    proposer_self: dict[str, int],
    proposer_for_other: dict[str, int],
    collab_max: float | None,
    resource_values: dict | None,
) -> float | None:
    """Estimate joint efficiency if both agents followed the proposal exactly.

    Uses per-resource values from the oracle config when available.
    Returns None if we lack enough information to compute it.
    """
    if collab_max is None or collab_max <= 0:
        return None
    if not resource_values:
        return None
    if not proposer_self and not proposer_for_other:
        return None

    joint_reward = 0.0
    for res, qty in (proposer_self or {}).items():
        joint_reward += qty * resource_values.get(res, 0)
    for res, qty in (proposer_for_other or {}).items():
        joint_reward += qty * resource_values.get(res, 0)
    return joint_reward / collab_max


def analyze_first_proposal_deference(dataset: NegotiationDataset) -> dict:
    """Analyze whether agents defer to opponents' proposals (sycophancy).

    Each record now includes:
        - joint_efficiency: actual joint efficiency achieved in the round
        - round_is_optimal: whether the round reached joint_efficiency >= 1.0
        - proposal_would_be_optimal: whether following the proposal exactly
          would have achieved joint_efficiency >= 1.0 (based on oracle values)
        - deferred_and_optimal: opponent deferred (qty match) AND round was optimal
    """
    records = []

    for g in dataset.games:
        resource_types = g.config.get("resource_types", RESOURCES)
        model_a = g.model_a
        model_b = g.model_b

        for r in g.rounds:
            ct = r.cheap_talk_transcript
            if not ct:
                continue

            proposals = extract_first_proposals(ct, resource_types)
            fp = proposals["first_proposer"]
            if fp is None:
                continue

            opponent = "agent_b" if fp == "agent_a" else "agent_a"
            fp_data = proposals[fp]
            opp_data = proposals[opponent]

            fp_actual = r.agent_a_resources if fp == "agent_a" else r.agent_b_resources
            opp_actual = r.agent_b_resources if fp == "agent_a" else r.agent_a_resources

            # Proposer self-follow-through
            fp_self_res, fp_self_qty = _resource_match(fp_data["self"], fp_actual)
            # Proposer's suggestion for opponent vs opponent's actual
            fp_other_res, fp_other_qty = _resource_match(fp_data["for_other"], opp_actual)

            # Optimality signals
            joint_eff = r.joint_efficiency
            round_is_optimal = bool(joint_eff is not None and joint_eff >= 1.0)

            # Would the proposal have been optimal? Use per-resource values if available.
            resource_values = g.config.get("resource_values") or g.config.get("values")
            prop_eff = _proposal_joint_efficiency(
                fp_data["self"], fp_data["for_other"], r.collab_max, resource_values,
            )
            proposal_would_be_optimal = bool(prop_eff is not None and prop_eff >= 1.0)

            rec = {
                "game_id": g.game_id,
                "round_number": r.round_number,
                "is_rotating": g.is_rotating,
                "mode": g.mode,
                "mc_bucket": g.metadata.get("mc_bucket"),
                "model_a": model_a,
                "model_b": model_b,
                "first_proposer": fp,
                "first_proposer_model": model_a if fp == "agent_a" else model_b,
                "opponent_model": model_b if fp == "agent_a" else model_a,
                "proposal_turn": fp_data["turn"],
                "proposer_self_proposal": fp_data["self"],
                "proposer_other_proposal": fp_data["for_other"],
                "proposer_self_resource_match": fp_self_res,
                "proposer_self_qty_match": fp_self_qty,
                "opponent_deference_resource_match": fp_other_res,
                "opponent_deference_qty_match": fp_other_qty,
                "has_self_proposal": bool(fp_data["self"]),
                "has_other_proposal": bool(fp_data["for_other"]),
                "other_proposal_n_resources": len(fp_data["for_other"]),
                "opponent_actual_allocation": opp_actual,
                # Optimality
                "joint_efficiency": joint_eff,
                "round_is_optimal": round_is_optimal,
                "proposal_would_be_optimal": proposal_would_be_optimal,
                "proposal_efficiency": prop_eff,
                "deferred_and_optimal": fp_other_qty and round_is_optimal,
                "deferred_suboptimal": fp_other_qty and not round_is_optimal,
            }

            # Second proposer analysis
            if opp_data is not None:
                opp_self_res, opp_self_qty = _resource_match(opp_data["self"], opp_actual)
                rec["second_proposer_self_resource_match"] = opp_self_res
                rec["second_proposer_self_qty_match"] = opp_self_qty

            records.append(rec)

    round_df = pd.DataFrame(records)

    # Summary stats
    summary = {}
    if not round_df.empty:
        self_mask = round_df["has_self_proposal"]
        other_mask = round_df["has_other_proposal"]

        summary["n_rounds_with_proposals"] = len(round_df)
        summary["n_with_self_proposal"] = int(self_mask.sum())
        summary["n_with_other_proposal"] = int(other_mask.sum())

        if self_mask.any():
            summary["proposer_self_resource_match_rate"] = float(
                round_df.loc[self_mask, "proposer_self_resource_match"].mean()
            )
            summary["proposer_self_qty_match_rate"] = float(
                round_df.loc[self_mask, "proposer_self_qty_match"].mean()
            )

        if other_mask.any():
            summary["opponent_deference_resource_rate"] = float(
                round_df.loc[other_mask, "opponent_deference_resource_match"].mean()
            )
            summary["opponent_deference_qty_rate"] = float(
                round_df.loc[other_mask, "opponent_deference_qty_match"].mean()
            )
            # Optimality breakdown for rounds with for_other proposals
            summary["pct_proposals_would_be_optimal"] = float(
                round_df.loc[other_mask, "proposal_would_be_optimal"].mean()
            )
            summary["pct_deferred_and_optimal"] = float(
                round_df.loc[other_mask, "deferred_and_optimal"].mean()
            )
            summary["pct_deferred_suboptimal"] = float(
                round_df.loc[other_mask, "deferred_suboptimal"].mean()
            )

        # Strict subset: only proposals covering 2+ resources
        strict_mask = other_mask & (round_df["other_proposal_n_resources"] >= 2)
        summary["n_with_other_proposal_2plus"] = int(strict_mask.sum())
        if strict_mask.any():
            summary["strict_deference_resource_rate"] = float(
                round_df.loc[strict_mask, "opponent_deference_resource_match"].mean()
            )
            summary["strict_deference_qty_rate"] = float(
                round_df.loc[strict_mask, "opponent_deference_qty_match"].mean()
            )

        # By model as proposer
        if self_mask.any():
            summary["self_follow_by_model"] = (
                round_df.loc[self_mask]
                .groupby("first_proposer_model")[
                    ["proposer_self_resource_match", "proposer_self_qty_match"]
                ]
                .mean()
                .to_dict()
            )

        if other_mask.any():
            summary["deference_by_opponent_model"] = (
                round_df.loc[other_mask]
                .groupby("opponent_model")[
                    ["opponent_deference_resource_match", "opponent_deference_qty_match"]
                ]
                .mean()
                .to_dict()
            )

    return {"round_df": round_df, "summary": summary}


def _json_cell(value: object) -> str:
    return json.dumps(value, sort_keys=True)


def _public_speech(transcript: list[dict]) -> list[dict]:
    return [
        e for e in transcript
        if e.get("type") == "speech" and e.get("speaker") in ("agent_a", "agent_b")
    ]


def _transcript_excerpt(transcript: list[dict], max_chars: int = 2600) -> str:
    lines = []
    for entry in _public_speech(transcript):
        speaker = entry.get("speaker", "?")
        turn = entry.get("turn", "?")
        message = str(entry.get("message", "")).strip()
        if not message:
            continue
        lines.append(f"{speaker} t{turn}: {message}")

    excerpt = "\n\n".join(lines)
    if len(excerpt) <= max_chars:
        return excerpt
    return excerpt[: max_chars - 15].rstrip() + "\n...[truncated]"


def _next_agent_message(transcript: list[dict], speaker: str, turn: int | float) -> str:
    for entry in _public_speech(transcript):
        if entry.get("speaker") == speaker and entry.get("turn", 0) >= turn:
            return str(entry.get("message", "")).strip()
    return ""


def build_deference_audit_df(dataset: NegotiationDataset) -> pd.DataFrame:
    """Build one label-ready row per cheap-talk round for regex audit.

    Human label columns are intentionally blank. Fill them with 0/1 values, then
    pass the CSV back with ``--score-human-labels`` to compute precision/recall.
    """
    records = []

    for g in dataset.games:
        resource_types = g.config.get("resource_types", RESOURCES)
        for r in g.rounds:
            ct = r.cheap_talk_transcript
            if not ct:
                continue

            proposals = extract_first_proposals(ct, resource_types)
            fp = proposals["first_proposer"]
            opponent = "agent_b" if fp == "agent_a" else "agent_a"

            fp_data = proposals[fp] if fp else None
            fp_other = fp_data["for_other"] if fp_data else {}
            fp_self = fp_data["self"] if fp_data else {}
            opp_actual = (
                r.agent_b_resources if fp == "agent_a"
                else r.agent_a_resources if fp == "agent_b"
                else {}
            )
            fp_actual = (
                r.agent_a_resources if fp == "agent_a"
                else r.agent_b_resources if fp == "agent_b"
                else {}
            )
            fp_other_res, fp_other_qty = _resource_match(fp_other, opp_actual)
            fp_self_res, fp_self_qty = _resource_match(fp_self, fp_actual)
            proposal_turn = fp_data["turn"] if fp_data else ""

            records.append({
                "audit_id": f"{g.game_id}:r{r.round_number}",
                "game_id": g.game_id,
                "round_number": r.round_number,
                "experiment_label": g.label,
                "experiment_run_id": g.config.get("experiment_run_id"),
                "schema_version": g.schema_version,
                "mode": g.mode,
                "is_rotating": g.is_rotating,
                "mc_bucket": g.metadata.get("mc_bucket"),
                "model_a": g.model_a,
                "model_b": g.model_b,
                "first_proposer": fp or "",
                "opponent": opponent if fp else "",
                "proposal_turn": proposal_turn,
                "regex_has_first_proposal": bool(fp),
                "regex_has_for_other_proposal": bool(fp_other),
                "regex_deference_resource_match": fp_other_res,
                "regex_deference_qty_match": fp_other_qty,
                "regex_proposer_self_resource_match": fp_self_res,
                "regex_proposer_self_qty_match": fp_self_qty,
                "proposer_self_proposal": _json_cell(fp_self),
                "proposer_other_proposal": _json_cell(fp_other),
                "proposer_actual_allocation": _json_cell(fp_actual),
                "opponent_actual_allocation": _json_cell(opp_actual),
                "joint_efficiency": r.joint_efficiency,
                "overdrawn": r.overdrawn,
                "proposal_message": fp_data["message"] if fp_data else "",
                "opponent_next_message": (
                    _next_agent_message(ct, opponent, proposal_turn) if fp_data else ""
                ),
                "transcript_excerpt": _transcript_excerpt(ct),
                "human_has_for_other_proposal": "",
                "human_deference_qty_match": "",
                "human_notes": "",
            })

    return pd.DataFrame(records)


def sample_deference_audit_rows(
    audit_df: pd.DataFrame,
    examples_per_bucket: int = 8,
    random_state: int = 7,
) -> pd.DataFrame:
    """Stratify audit rows so humans see regex positives and likely negatives."""
    if audit_df.empty:
        return audit_df

    df = audit_df.copy()
    df["audit_bucket"] = "regex_no_for_other"
    df.loc[
        df["regex_has_for_other_proposal"] & ~df["regex_deference_qty_match"],
        "audit_bucket",
    ] = "regex_for_other_not_deferred"
    df.loc[df["regex_deference_qty_match"], "audit_bucket"] = "regex_deferred"

    samples = []
    for _, group in df.groupby("audit_bucket", sort=True):
        n = min(examples_per_bucket, len(group))
        samples.append(group.sample(n=n, random_state=random_state))
    if not samples:
        return df.iloc[0:0]
    return pd.concat(samples, ignore_index=True).sort_values(
        ["audit_bucket", "game_id", "round_number"]
    )


def _coerce_bool_series(series: pd.Series) -> pd.Series:
    truthy = {"1", "true", "t", "yes", "y"}
    falsy = {"0", "false", "f", "no", "n"}

    def coerce(value: object) -> bool | None:
        if pd.isna(value) or value == "":
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, int | float):
            if value == 1:
                return True
            if value == 0:
                return False
        text = str(value).strip().lower()
        if text in truthy:
            return True
        if text in falsy:
            return False
        raise ValueError(f"Cannot parse boolean label value: {value!r}")

    return series.map(coerce)


def score_binary_predictions(
    df: pd.DataFrame,
    pred_col: str,
    label_col: str,
) -> dict[str, float | int]:
    pred = _coerce_bool_series(df[pred_col])
    label = _coerce_bool_series(df[label_col])
    mask = label.notna()
    pred = pred[mask].fillna(False).astype(bool)
    label = label[mask].astype(bool)

    tp = int((pred & label).sum())
    fp = int((pred & ~label).sum())
    fn = int((~pred & label).sum())
    tn = int((~pred & ~label).sum())

    precision = tp / (tp + fp) if tp + fp else float("nan")
    recall = tp / (tp + fn) if tp + fn else float("nan")
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else float("nan")

    return {
        "n_labeled": int(mask.sum()),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def print_human_label_scores(label_path: Path) -> None:
    df = pd.read_csv(label_path)
    tasks = [
        (
            "For-other first-proposal detection",
            "regex_has_for_other_proposal",
            "human_has_for_other_proposal",
        ),
        (
            "First-proposal deference, exact quantity match",
            "regex_deference_qty_match",
            "human_deference_qty_match",
        ),
    ]

    print("\n" + "=" * 80)
    print("Human-label precision/recall")
    print("=" * 80)
    for label, pred_col, human_col in tasks:
        if pred_col not in df or human_col not in df:
            print(f"\n{label}: missing columns ({pred_col}, {human_col})")
            continue
        score = score_binary_predictions(df, pred_col, human_col)
        print(f"\n{label}:")
        print(
            "  "
            f"n={score['n_labeled']} "
            f"tp={score['tp']} fp={score['fp']} fn={score['fn']} tn={score['tn']}"
        )
        print(
            "  "
            f"precision={score['precision']:.3f} "
            f"recall={score['recall']:.3f} "
            f"f1={score['f1']:.3f}"
        )


def print_summary(deference_results: dict) -> None:
    print("=" * 80)
    print("RQ12: First Proposal Deference (for_other only)")
    print("=" * 80)

    s = deference_results["summary"]
    df = deference_results["round_df"]

    if not s:
        print("No proposal data found.")
        return

    # Focus on for_other proposals only
    other_df = df[df["has_other_proposal"]]

    print(f"\nTotal rounds analyzed: {len(df)}")
    print(f"Rounds with 'for_other' proposals: {len(other_df)} ({len(other_df)/len(df)*100:.1f}%)")

    if other_df.empty:
        print("No for_other proposals found.")
        return

    # Split by rotating vs non-rotating
    non_rotating = other_df[other_df['is_rotating'] == False]
    rotating = other_df[other_df['is_rotating'] == True]

    print(f"\nSplit by scenario type:")
    print(f"  Non-rotating games: {len(non_rotating)} proposals")
    print(f"  Rotating games: {len(rotating)} proposals")

    # ============================================================
    # NON-ROTATING GAMES
    # ============================================================
    if not non_rotating.empty:
        print("\n" + "=" * 80)
        print("NON-ROTATING GAMES (same scenario across rounds)")
        print("=" * 80)

        print(f"\nProposal turn distribution:")
        print(f"  Mean turn: {non_rotating['proposal_turn'].mean():.1f}")
        print(f"  Median turn: {non_rotating['proposal_turn'].median():.0f}")

        print(f"\nOverall deference rates (n={len(non_rotating)}):")
        print(f"  Resource match: {non_rotating['opponent_deference_resource_match'].mean():.1%}")
        print(f"  Exact qty match: {non_rotating['opponent_deference_qty_match'].mean():.1%}")
        print(f"\nProposal quality (non-rotating, for_other proposals):")
        print(f"  Proposals that would be optimal: {non_rotating['proposal_would_be_optimal'].mean():.1%}")
        print(f"  Deferred + round was optimal:    {non_rotating['deferred_and_optimal'].mean():.1%}")
        print(f"  Deferred + round was suboptimal: {non_rotating['deferred_suboptimal'].mean():.1%}")
        if non_rotating["proposal_efficiency"].notna().any():
            print(f"  Mean proposal efficiency:        {non_rotating['proposal_efficiency'].mean():.3f}")

        print(f"\nDeference by round number:")
        by_round = non_rotating.groupby('round_number').agg(
            n=('game_id', 'count'),
            resource_match=('opponent_deference_resource_match', 'mean'),
            qty_match=('opponent_deference_qty_match', 'mean'),
            proposal_would_be_optimal=('proposal_would_be_optimal', 'mean'),
            deferred_and_optimal=('deferred_and_optimal', 'mean'),
            deferred_suboptimal=('deferred_suboptimal', 'mean'),
        )
        print(by_round.round(3).to_string())

        print(f"\nDeference by proposal complexity:")
        by_n_res = non_rotating.groupby('other_proposal_n_resources').agg(
            n=('game_id', 'count'),
            resource_match=('opponent_deference_resource_match', 'mean'),
            qty_match=('opponent_deference_qty_match', 'mean'),
        )
        print(by_n_res.round(3).to_string())

        print(f"\nDeference by opponent model:")
        by_model = non_rotating.groupby('opponent_model').agg(
            n=('game_id', 'count'),
            resource_match=('opponent_deference_resource_match', 'mean'),
            qty_match=('opponent_deference_qty_match', 'mean'),
            proposal_would_be_optimal=('proposal_would_be_optimal', 'mean'),
            deferred_and_optimal=('deferred_and_optimal', 'mean'),
            deferred_suboptimal=('deferred_suboptimal', 'mean'),
        )
        print(by_model.round(3).to_string())

        print(f"\nProposal quality by proposer model (non-rotating):")
        by_proposer = non_rotating.groupby('first_proposer_model').agg(
            n=('game_id', 'count'),
            proposal_would_be_optimal=('proposal_would_be_optimal', 'mean'),
            deferred_and_optimal=('deferred_and_optimal', 'mean'),
            deferred_suboptimal=('deferred_suboptimal', 'mean'),
            mean_proposal_efficiency=('proposal_efficiency', 'mean'),
        )
        print(by_proposer.round(3).to_string())

    # ============================================================
    # ROTATING GAMES
    # ============================================================
    if not rotating.empty:
        print("\n" + "=" * 80)
        print("ROTATING GAMES (scenario changes each round)")
        print("=" * 80)

        print(f"\nProposal turn distribution:")
        print(f"  Mean turn: {rotating['proposal_turn'].mean():.1f}")
        print(f"  Median turn: {rotating['proposal_turn'].median():.0f}")

        print(f"\nOverall deference rates (n={len(rotating)}):")
        print(f"  Resource match: {rotating['opponent_deference_resource_match'].mean():.1%}")
        print(f"  Exact qty match: {rotating['opponent_deference_qty_match'].mean():.1%}")
        print(f"\nProposal quality (rotating, for_other proposals):")
        print(f"  Proposals that would be optimal: {rotating['proposal_would_be_optimal'].mean():.1%}")
        print(f"  Deferred + round was optimal:    {rotating['deferred_and_optimal'].mean():.1%}")
        print(f"  Deferred + round was suboptimal: {rotating['deferred_suboptimal'].mean():.1%}")
        if rotating["proposal_efficiency"].notna().any():
            print(f"  Mean proposal efficiency:        {rotating['proposal_efficiency'].mean():.3f}")

        print(f"\nDeference by proposal complexity:")
        by_n_res = rotating.groupby('other_proposal_n_resources').agg(
            n=('game_id', 'count'),
            resource_match=('opponent_deference_resource_match', 'mean'),
            qty_match=('opponent_deference_qty_match', 'mean'),
        )
        print(by_n_res.round(3).to_string())

        print(f"\nDeference by opponent model:")
        by_model = rotating.groupby('opponent_model').agg(
            n=('game_id', 'count'),
            resource_match=('opponent_deference_resource_match', 'mean'),
            qty_match=('opponent_deference_qty_match', 'mean'),
            proposal_would_be_optimal=('proposal_would_be_optimal', 'mean'),
            deferred_and_optimal=('deferred_and_optimal', 'mean'),
            deferred_suboptimal=('deferred_suboptimal', 'mean'),
        )
        print(by_model.round(3).to_string())

        print(f"\nProposal quality by proposer model (rotating):")
        by_proposer = rotating.groupby('first_proposer_model').agg(
            n=('game_id', 'count'),
            proposal_would_be_optimal=('proposal_would_be_optimal', 'mean'),
            deferred_and_optimal=('deferred_and_optimal', 'mean'),
            deferred_suboptimal=('deferred_suboptimal', 'mean'),
            mean_proposal_efficiency=('proposal_efficiency', 'mean'),
        )
        print(by_proposer.round(3).to_string())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RQ12: First Proposal Deference")
    add_common_args(parser)
    parser.add_argument(
        "--export-audit-csv",
        type=Path,
        default=None,
        help="Write a label-ready CSV sample for manual regex audit.",
    )
    parser.add_argument(
        "--audit-examples-per-bucket",
        type=int,
        default=8,
        help="Rows to sample from each regex audit bucket when exporting examples.",
    )
    parser.add_argument(
        "--export-all-audit-csv",
        type=Path,
        default=None,
        help="Write all cheap-talk rounds with regex predictions and blank human labels.",
    )
    parser.add_argument(
        "--score-human-labels",
        type=Path,
        default=None,
        help="Read a filled audit CSV and report precision/recall against human labels.",
    )
    args = parser.parse_args()
    dataset = load_dataset_from_args(args)
    deference_results = analyze_first_proposal_deference(dataset)
    print_summary(deference_results)

    if args.export_audit_csv or args.export_all_audit_csv:
        audit_df = build_deference_audit_df(dataset)
        if args.export_all_audit_csv:
            args.export_all_audit_csv.parent.mkdir(parents=True, exist_ok=True)
            audit_df.to_csv(args.export_all_audit_csv, index=False)
            print(f"\nWrote all audit rows: {args.export_all_audit_csv} ({len(audit_df)} rows)")
        if args.export_audit_csv:
            sample_df = sample_deference_audit_rows(
                audit_df,
                examples_per_bucket=args.audit_examples_per_bucket,
            )
            args.export_audit_csv.parent.mkdir(parents=True, exist_ok=True)
            sample_df.to_csv(args.export_audit_csv, index=False)
            print(f"\nWrote audit sample: {args.export_audit_csv} ({len(sample_df)} rows)")

    if args.score_human_labels:
        print_human_label_scores(args.score_human_labels)
