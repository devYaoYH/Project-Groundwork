"""Aggregation module: flatten judgments into a one-row-per-environment CSV.

Schema: one row per environment, with flat environment-level columns + a single
`rounds_summary` JSON column holding per-round breakdowns.

Pattern classification uses a deterministic alias lookup built from the
Taxonomy (no additional LLM call). Patterns whose free-text name does not
match any canonical's `aliases` list are tagged canonical_id="other".
"""

import json
import logging
from pathlib import Path
from typing import Optional

import pandas as pd

from negotiation_judge.schema import (
    Effectiveness,
    GameJudgment,
    RoundJudgment,
    Taxonomy,
)

log = logging.getLogger("judge.aggregate")


# --- Alias / canonical lookup ---

def _normalize(s: str) -> str:
    """Case-insensitive, whitespace-trimmed lookup key."""
    return " ".join(s.lower().split()) if s else ""


def build_alias_index(taxonomy: Taxonomy) -> dict[str, tuple[str, str]]:
    """Build {normalized_alias: (canonical_id, bucket)} where bucket is
    'negative_pattern' or 'positive_pattern'.

    Also maps the canonical label and id themselves (in case the pattern
    name already uses the canonical form).
    """
    index: dict[str, tuple[str, str]] = {}

    def _add(name: str, canonical_id: str, bucket: str):
        key = _normalize(name)
        if key and key not in index:
            index[key] = (canonical_id, bucket)

    for p in taxonomy.negative_patterns:
        _add(p.id, p.id, "negative_pattern")
        _add(p.label, p.id, "negative_pattern")
        for alias in p.aliases:
            _add(alias, p.id, "negative_pattern")
    for p in taxonomy.positive_patterns:
        _add(p.id, p.id, "positive_pattern")
        _add(p.label, p.id, "positive_pattern")
        for alias in p.aliases:
            _add(alias, p.id, "positive_pattern")

    return index


def classify_pattern_name(
    name: str,
    alias_index: dict[str, tuple[str, str]],
) -> tuple[str, Optional[str]]:
    """Map a free-text pattern name to (canonical_id, bucket).

    Returns ("other", None) if no alias matches.
    """
    if not alias_index:
        return "other", None
    hit = alias_index.get(_normalize(name))
    if hit is None:
        return "other", None
    return hit


# --- Per-round summary ---

def _summarize_round(
    rj: RoundJudgment,
    alias_index: dict[str, tuple[str, str]],
) -> dict:
    """Build a single round's summary dict for embedding in rounds_summary JSON."""
    n_pos = n_neg = n_neu = 0
    negative_patterns: list[dict] = []
    positive_patterns: list[dict] = []
    other_patterns: list[dict] = []

    for p in rj.patterns:
        # Count by instance-level effectiveness
        if p.effectiveness == Effectiveness.positive:
            n_pos += 1
        elif p.effectiveness == Effectiveness.negative:
            n_neg += 1
        else:
            n_neu += 1

        # Map pattern_name to canonical bucket via alias index
        canonical_id, bucket = classify_pattern_name(p.name, alias_index)
        entry = {
            "name": p.name,
            "canonical_id": canonical_id,
            "effectiveness": p.effectiveness.value,
            "intent": p.intent.value,
        }
        if bucket == "negative_pattern":
            negative_patterns.append(entry)
        elif bucket == "positive_pattern":
            positive_patterns.append(entry)
        else:
            other_patterns.append(entry)

    return {
        "round_number": rj.round_number,
        "round_outcome": rj.round_outcome.value,
        "joint_efficiency": rj.joint_efficiency,
        "n_positive_instances": n_pos,
        "n_negative_instances": n_neg,
        "n_neutral_instances": n_neu,
        "negative_patterns": negative_patterns,
        "positive_patterns": positive_patterns,
        "other_patterns": other_patterns,
        "round_attribution": rj.round_attribution,
        "prior_round_influence": rj.prior_round_influence,
    }


# --- Public API ---

def build_judgments_csv(
    judgments,
    output_path: Path,
    taxonomy: Optional[Taxonomy] = None,
    contexts: Optional[list] = None,
) -> pd.DataFrame:
    """Build a one-row-per-environment CSV.

    Accepts list[GameJudgment] or list[RoundJudgment] (legacy per-round,
    grouped by episode_uid).

    If `taxonomy` is provided, pattern_name → canonical_id is resolved
    deterministically via alias matching. Otherwise every pattern is tagged
    canonical_id="other".

    If `contexts` is provided (list of JudgeGameContext), shifting_agent and
    oracle_optimum are pulled from the matching context.

    Columns:
        episode_uid, model_a, model_b, mode, shifting_agent, mc_ratio,
        oracle_optimum, num_rounds, avg_joint_efficiency,
        total_positive_instances, total_negative_instances, total_neutral_instances,
        unique_negative_patterns, unique_positive_patterns,
        rounds_summary (JSON-encoded list of round dicts),
        game_attribution
    """
    alias_index = build_alias_index(taxonomy) if taxonomy else {}

    # Build context lookup for shifting_agent and oracle_optimum
    ctx_lookup: dict[str, dict] = {}
    if contexts:
        for ctx in contexts:
            ctx_lookup[ctx.episode_uid] = {
                "shifting_agent": ctx.shifting_agent,
                "oracle_optimum": ctx.oracle_optimum,
            }

    # Normalize input: group into episode_uid → (game_meta, [round_judgments])
    games: dict[str, dict] = {}

    for j in judgments:
        if isinstance(j, GameJudgment):
            gid = j.episode_uid
            ctx_info = ctx_lookup.get(gid, {})
            games.setdefault(gid, {
                "episode_uid": j.episode_uid,
                "model_a": j.model_a,
                "model_b": j.model_b,
                "mode": j.mode,
                "shifting_agent": ctx_info.get("shifting_agent"),
                "mc_ratio": j.mc_ratio,
                "oracle_optimum": ctx_info.get("oracle_optimum"),
                "game_attribution": j.game_attribution,
                "rounds": [],
            })
            games[gid]["rounds"].extend(j.rounds)
        else:
            # Legacy per-round: group by episode_uid
            gid = j.episode_uid
            ctx_info = ctx_lookup.get(gid, {})
            games.setdefault(gid, {
                "episode_uid": j.episode_uid,
                "model_a": j.model_a,
                "model_b": j.model_b,
                "mode": j.mode,
                "shifting_agent": ctx_info.get("shifting_agent"),
                "mc_ratio": j.mc_ratio,
                "oracle_optimum": ctx_info.get("oracle_optimum"),
                "game_attribution": None,
                "rounds": [],
            })
            games[gid]["rounds"].append(j)

    rows = []
    for gid, g in games.items():
        rounds_sorted = sorted(g["rounds"], key=lambda r: r.round_number)
        round_summaries = [_summarize_round(r, alias_index) for r in rounds_sorted]

        # Environment-level aggregates
        total_pos = sum(r["n_positive_instances"] for r in round_summaries)
        total_neg = sum(r["n_negative_instances"] for r in round_summaries)
        total_neu = sum(r["n_neutral_instances"] for r in round_summaries)

        all_fm_ids: list[str] = []
        all_pp_ids: list[str] = []
        for r in round_summaries:
            all_fm_ids.extend(p["canonical_id"] for p in r["negative_patterns"])
            all_pp_ids.extend(p["canonical_id"] for p in r["positive_patterns"])
        unique_fm = sorted(set(all_fm_ids))
        unique_pp = sorted(set(all_pp_ids))

        # Average joint efficiency across rounds (ignore NaN)
        effs = [r["joint_efficiency"] for r in round_summaries
                if r["joint_efficiency"] is not None and r["joint_efficiency"] == r["joint_efficiency"]]
        avg_eff = sum(effs) / len(effs) if effs else None

        rows.append({
            "episode_uid": g["episode_uid"],
            "model_a": g["model_a"],
            "model_b": g["model_b"],
            "mode": g["mode"],
            "shifting_agent": g["shifting_agent"],
            "mc_ratio": g["mc_ratio"],
            "oracle_optimum": g["oracle_optimum"],
            "num_rounds": len(round_summaries),
            "avg_joint_efficiency": avg_eff,
            "total_positive_instances": total_pos,
            "total_negative_instances": total_neg,
            "total_neutral_instances": total_neu,
            "unique_negative_patterns": json.dumps(unique_fm),
            "unique_positive_patterns": json.dumps(unique_pp),
            "rounds_summary": json.dumps(round_summaries),
            "game_attribution": g["game_attribution"],
        })

    df = pd.DataFrame(rows)
    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(output_path, index=False)
        log.info("Wrote %d environment rows to %s", len(df), output_path)
    return df


def load_judgments_csv(path: Path) -> pd.DataFrame:
    """Load the per-environment CSV. `rounds_summary`, `unique_negative_patterns`,
    and `unique_positive_patterns` columns remain JSON strings; call
    `json.loads` in your notebook to work with them as Python objects.
    """
    return pd.read_csv(path)


def load_raw_judgments(output_dir: Path) -> list:
    """Load all raw (Phase 1) judgments from the output directory.

    Auto-detects whether files are GameJudgment or legacy RoundJudgment.
    """
    raw_dir = output_dir / "raw"
    judgments: list = []
    if not raw_dir.exists():
        return judgments
    for f in sorted(raw_dir.glob("*.json")):
        try:
            with open(f) as fh:
                data = json.load(fh)
            is_game = "rounds" in data and isinstance(data["rounds"], list)
            if is_game:
                judgments.append(GameJudgment.model_validate(data))
            else:
                judgments.append(RoundJudgment.model_validate(data))
        except Exception as e:
            log.warning("Skipping %s: %s", f, e)
    return judgments
