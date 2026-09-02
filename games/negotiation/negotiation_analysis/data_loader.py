"""Load and classify game data from Firestore (or cached JSON) into DataFrames.

Primary source: Firestore experiment traces (30 labeled games).
Fallback: cached JSON file at data/experiment_traces.json.

Usage:
    from negotiation_analysis.data_loader import load_experiment_data, build_round_df, build_turn_df

    raw_games = load_experiment_data()
    round_df = build_round_df(raw_games)
    turn_df = build_turn_df(raw_games)
"""

import argparse
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path

import pandas as pd

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from negotiation_game.backend.storage import firestore_available, list_traces, get_trace
from negotiation_game.backend.defaults import REPO_ROOT
from negotiation_analysis.models import NegotiationDataset

log = logging.getLogger("data_loader")

# 720-game main cohort used in the paper (4 run IDs + 3 tombstoned incomplete games)
MAIN_COHORT_RUN_IDS: frozenset[str] = frozenset({
    "8aab2461-2450-4781-b341-51e54a653122",  # self-play, run A
    "37a7488c-ea18-4b15-bfe7-7978a4ecbb8b",  # self-play, run B
    "6cb004cb-1097-4c87-9003-679a41343733",  # cross-play
    "0c040ed0-4a4c-448f-b10e-6c338ff1f035",  # GPT-5 Mini self-play backfill
})
FULL_TRANSPARENCY_COHORT_RUN_IDS: frozenset[str] = frozenset({
    "56abe7a8-2d59-4b7b-9d4d-80cbdd078a65",  # Qwen 3.5 Flash x GPT-5 Mini, initial 100 games
    "ac21edea-41ae-4953-894c-0327927b0e8a",  # Qwen 3.5 Flash x GPT-5 Mini, remaining stable M/C=0.8 cells
    "cae3ddf2-d9af-4b3f-9af1-82280c21acb1",  # Claude Sonnet 4.5 x Claude Sonnet 4.5
})
TOMBSTONED_GAME_IDS: frozenset[str] = frozenset({
    "1e0a1120",
    "7cde50c3",
    "abe546f3",
})

RESOURCES = ["wood", "stone", "gold"]
RESOURCE_COSTS = {"wood": 1.0, "stone": 1.5, "gold": 3.0}
RESOURCE_SUPPLY = {"wood": 10, "stone": 10, "gold": 6}
BUDGET = 15.0

# The game's data lives beside the packages that ship it, not at the root of
# whatever repository vendored them. Resolving against REPO_ROOT pointed the
# denylist at a path that does not exist, and a missing denylist silently
# filters nothing, so excluded games quietly rejoined the analysis.
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_PATH = DATA_DIR / "experiment_traces.json"
DENYLIST_PATH = DATA_DIR / "denylist.txt"


# ---------------------------------------------------------------------------
# Denylist
# ---------------------------------------------------------------------------

def load_denylist(path: Path | None = None) -> set[str]:
    """Load game ID prefixes to exclude from analysis.

    Each non-comment line starts with a game_id prefix. Anything after the
    first whitespace is treated as a comment/reason.
    """
    path = path or DENYLIST_PATH
    if not path.exists():
        return set()
    prefixes = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        prefixes.add(line.split()[0])
    return prefixes


def apply_denylist(games: list[dict], denylist: set[str] | None = None) -> list[dict]:
    """Remove games whose game_id starts with any denylisted prefix."""
    if denylist is None:
        denylist = load_denylist()
    if not denylist:
        return games
    before = len(games)
    filtered = [g for g in games if not any(g["game_id"].startswith(p) for p in denylist)]
    removed = before - len(filtered)
    if removed > 0:
        log.info("Denylist removed %d/%d games", removed, before)
    return filtered


# ---------------------------------------------------------------------------
# Experiment label parsing
# ---------------------------------------------------------------------------

def parse_experiment_label(label: str) -> dict:
    """Parse an experiment label into its 4 condition dimensions.

    Labels follow the format: {mode}_{goal_type}_{thinking_mode}[_no_outcome]

    Examples:
        stable_collaborative_hidden_thinking           → implicit outcome
        shifting_competitive_shown_thinking_no_outcome  → no_outcome
    """
    parts = label.split("_")
    mode = parts[0] if len(parts) >= 1 else "unknown"
    goal_type = parts[1] if len(parts) >= 2 else "unknown"
    # thinking_mode is two tokens: e.g. "hidden_thinking" or "shown_thinking"
    thinking_mode = "_".join(parts[2:4]) if len(parts) >= 4 else "unknown"
    outcome_visibility = "no_outcome" if label.endswith("_no_outcome") else "implicit"
    return {
        "mode": mode,
        "goal_type": goal_type,
        "thinking_mode": thinking_mode,
        "outcome_visibility": outcome_visibility,
    }


def parse_v5_label(label: str) -> dict:
    """Parse a V5+ experiment label into its dimensions.

    V5+ labels follow: {model}_{mode}_{fixed|rotate}[_share][_tom]_mc{05|08|10}

    Examples:
        haiku_stable_rotate_mc08
        sonnet45_shifting_rotate_share_tom_mc08
        gpt54m_shifting_fixed_mc05
    """
    parts = label.split("_")
    mc_bucket = None
    mode = "unknown"
    rotation = "unknown"
    share_projects = False
    think_about_opponent = False
    named_projects = False
    model_name = "unknown"

    # Extract mc bucket from end (mc05, mc08, mc10)
    if parts and parts[-1].startswith("mc"):
        mc_str = parts[-1][2:]  # e.g. "05", "08", "10"
        try:
            mc_bucket = int(mc_str) / 10.0  # 0.5, 0.8, 1.0
        except ValueError:
            pass

    # Find mode (stable/shifting) — determines where model name ends
    for i, p in enumerate(parts):
        if p in ("stable", "shifting"):
            model_name = "_".join(parts[:i])
            mode = p
            rest = parts[i + 1:]
            break
    else:
        return {"model": model_name, "mode": mode, "rotation": rotation,
                "mc_bucket": mc_bucket, "share_projects": share_projects,
                "think_about_opponent": think_about_opponent,
                "named_projects": named_projects}

    # Parse rest (before mc suffix): fixed/rotate, share, tom
    for p in rest:
        if p in ("fixed", "rotate"):
            rotation = p
        elif p == "share":
            share_projects = True
        elif p == "tom":
            think_about_opponent = True
        elif p == "named":
            named_projects = True

    return {
        "model": model_name,
        "mode": mode,
        "rotation": rotation,
        "mc_bucket": mc_bucket,
        "share_projects": share_projects,
        "think_about_opponent": think_about_opponent,
        "named_projects": named_projects,
    }


def get_agent_models(game: dict) -> tuple[str, str]:
    """Extract model names for agent_a and agent_b from agent configs.

    Primary: reads from agents[].model in game config.
    Fallback: parses model from experiment label via parse_v5_label().
    Logs a warning when falling back so the trace can be investigated.

    Returns (model_a, model_b).
    """
    agents = game.get("agents", [])
    model_a = agents[0].get("model") if len(agents) > 0 else None
    model_b = agents[1].get("model") if len(agents) > 1 else None

    if not model_a or not model_b:
        label = game.get("experiment_label", "")
        fallback = parse_v5_label(label).get("model", "unknown") if label else "unknown"
        game_id = game.get("game_id", "?")
        if not model_a:
            log.warning(
                "game %s agent_a missing model in config, falling back to label parse: %s",
                game_id, fallback,
            )
            model_a = fallback
        if not model_b:
            log.warning(
                "game %s agent_b missing model in config, falling back to label parse: %s",
                game_id, fallback,
            )
            model_b = fallback

    return model_a, model_b


def get_round_oracle(game: dict, round_idx: int) -> dict | None:
    """Get oracle stats for a specific round.

    For rotating games, uses per_round_scenarios[round_idx].oracle_stats.
    Falls back to game-level oracle_stats.
    """
    prs = game.get("per_round_scenarios")
    if prs and round_idx < len(prs):
        oracle = prs[round_idx].get("oracle_stats")
        if oracle:
            return oracle
    return game.get("oracle_stats")


# ---------------------------------------------------------------------------
# Trace → raw_game conversion
# ---------------------------------------------------------------------------

def reconstruct_transcript_from_events(events: list[dict], round_num: int) -> list[dict]:
    """Reconstruct cheap_talk_transcript from events for a given round.

    Args:
        events: List of game events
        round_num: Round number to reconstruct transcript for

    Returns:
        List of transcript entries (thinking + cheap_talk), sorted by turn
    """
    transcript = []

    for event in events:
        if event["type"] == "thinking":
            data = event["data"]
            if data.get("round") == round_num:
                entry = {
                    "speaker": data["agent"],
                    "turn": data["turn"],
                    "type": "thinking",
                    "message": data["content"],
                }
                if "api_meta" in data:
                    entry["api_meta"] = data["api_meta"]
                transcript.append(entry)

        elif event["type"] == "cheap_talk":
            data = event["data"]
            if data.get("round") == round_num:
                entry = {
                    "speaker": data["speaker"],
                    "turn": data["turn"],
                    "type": data.get("type", "speech"),
                    "message": data["message"],
                }
                transcript.append(entry)

        elif event["type"] == "early_decision":
            # Early decision events contain allocation submitted mid-cheap-talk
            data = event["data"]
            if data.get("round") == round_num:
                # This is already captured in cheap_talk events, so we can skip
                # (the engine emits both cheap_talk and early_decision)
                pass

    # Sort by turn number (events may be out of order)
    transcript.sort(key=lambda x: x.get("turn", 0))
    return transcript


def _trace_to_game(trace: dict) -> dict:
    """Convert a Firestore trace dict into the standardised raw_game format.

    Supports all schema versions:
    - V1: transcript in result.rounds[].cheap_talk_transcript (legacy)
    - V2: transcript reconstructed from events (uncompressed)
    - V3: transcript reconstructed from events_compressed (gzip)
    """
    schema_version = trace.get("schema_version", 1)
    config = trace.get("game_config", {})
    result = trace.get("result", {})

    # V3: Decompress events if compressed
    if "events_compressed" in trace:
        from negotiation_game.backend.storage import decompress_events
        events = decompress_events(trace["events_compressed"])
    else:
        events = trace.get("events", [])

    game_id = trace.get("game_id", result.get("game_id", ""))

    experiment_label = config.get("experiment_label", "")
    parsed = parse_experiment_label(experiment_label)

    mode = parsed["mode"]
    goal_type = parsed["goal_type"]
    thinking_mode = parsed["thinking_mode"]
    outcome_visibility = parsed["outcome_visibility"]
    condition = f"{mode}_{goal_type}"

    agent_values = config.get("agent_values", result.get("agent_values", [{}, {}]))
    a_values = agent_values[0] if len(agent_values) > 0 else {}
    b_values = agent_values[1] if len(agent_values) > 1 else {}

    rounds = result.get("rounds", [])

    # Schema V2/V3: Reconstruct transcript from events if not present in rounds
    if schema_version >= 2:
        for rnd in rounds:
            if not rnd.get("cheap_talk_transcript"):
                round_num = rnd["round_number"]
                rnd["cheap_talk_transcript"] = reconstruct_transcript_from_events(
                    events, round_num
                )
    # Schema V1: Transcript already in rounds, use directly (no action needed)

    has_thinking = any(
        entry.get("type") == "thinking"
        for r in rounds
        for entry in (r.get("cheap_talk_transcript") or [])
    )

    agents = config.get("agents", [])
    resource_costs = config.get("resource_costs", RESOURCE_COSTS)
    resource_types = config.get("resource_types", RESOURCES)

    return {
        "game_id": game_id,
        "experiment_label": experiment_label,
        "mode": mode,
        "goal_type": goal_type,
        "thinking_mode": thinking_mode,
        "outcome_visibility": outcome_visibility,
        "condition": condition,
        "has_thinking": has_thinking,
        "agent_a_values": a_values,
        "agent_b_values": b_values,
        "num_rounds": len(rounds),
        "rounds": rounds,
        "seed": config.get("seed"),
        "swapped": config.get("swapped", False),
        "first_speaker": config.get("first_speaker", 0),
        "agent_a_cumulative_reward": result.get("agent_a_cumulative_reward", 0),
        "agent_b_cumulative_reward": result.get("agent_b_cumulative_reward", 0),
        "agents": agents,
        "resource_costs": resource_costs,
        "resource_types": resource_types,
        "agent_budget": config.get("agent_budget", BUDGET),
        "cheap_talk_turns": config.get("cheap_talk_turns", 3),
        "enable_cheap_talk": config.get("enable_cheap_talk", True),
        "created_at": trace.get("created_at", ""),
        # Experiment tracking metadata (V4+)
        "experiment_run_id": config.get("experiment_run_id"),
        "experiment_name": config.get("experiment_name"),
        "git_hash": config.get("git_hash"),
        # V5 project-based fields
        "agent_projects": config.get("agent_projects"),
        "oracle_stats": config.get("oracle_stats"),
        "per_round_scenarios": result.get("per_round_scenarios"),
        "reflections": result.get("reflections"),
        "schema_version": schema_version,
        "events": events,
    }


# ---------------------------------------------------------------------------
# Data loading functions
# ---------------------------------------------------------------------------

def load_from_firestore(filters: dict | None = None) -> list[dict]:
    """Fetch labeled experiment traces from Firestore and convert to raw_games.

    Args:
        filters: Optional dict of Firestore field filters (e.g., {"schema_version": 2})
    """
    if not firestore_available():
        raise RuntimeError("Firestore is not available")

    all_traces = []
    cursor = None
    page_size = 50

    while True:
        batch, _ = list_traces(limit=page_size, start_after=cursor, filters=filters)
        if not batch:
            break
        for summary in batch:
            label = summary.get("experiment_label", "")
            if not label:
                continue
            trace = get_trace(summary["game_id"])
            if trace:
                all_traces.append(trace)
        if len(batch) < page_size:
            break
        cursor = batch[-1]["game_id"]

    games = [_trace_to_game(t) for t in all_traces]
    log.info("Loaded %d labeled games from Firestore (filters=%s)", len(games), filters)
    return games


def cache_traces(games_or_traces: list[dict], path: Path = CACHE_PATH) -> None:
    """Save raw Firestore traces to a local JSON cache file."""
    os.makedirs(path.parent, exist_ok=True)
    with open(path, "w") as f:
        json.dump(games_or_traces, f, indent=2, default=str)
    log.info("Cached %d traces to %s", len(games_or_traces), path)


def load_from_cache(path: Path = CACHE_PATH) -> list[dict]:
    """Load raw_games from the cached JSON file."""
    if not path.is_file():
        raise FileNotFoundError(f"Cache file not found: {path}")
    with open(path) as f:
        data = json.load(f)
    # The cache may contain either raw traces or already-converted games.
    # If entries have "game_config", they're raw traces; convert them.
    if data and "game_config" in data[0]:
        games = [_trace_to_game(t) for t in data]
    else:
        games = data
    log.info("Loaded %d games from cache: %s", len(games), path)
    return games


def load_experiment_data(
    filters: dict | None = None,
    cache_path: Path | None = None,
    apply_deny: bool = True,
    schema_version: int | None = None,
) -> list[dict]:
    """Load experiment data: try Firestore first, then cached JSON.

    Args:
        filters: Optional dict of Firestore field filters (e.g., {"schema_version": 2})
        cache_path: Optional custom cache file path (defaults to CACHE_PATH)
        apply_deny: If True (default), remove games listed in data/denylist.txt
        schema_version: If provided, filter to only this schema version (applied after loading)

    Returns a list of raw_game dicts with standardised fields including
    experiment_label, thinking_mode, outcome_visibility, etc.
    """
    if cache_path is None:
        cache_path = CACHE_PATH

    games = None

    # Try Firestore
    try:
        games = load_from_firestore(filters=filters)
        if games:
            # Auto-cache for offline use
            cache_traces(games, cache_path)
    except Exception as e:
        log.info("Firestore unavailable (%s), falling back to cache", e)

    # Try cache
    if not games:
        try:
            games = load_from_cache(cache_path)
        except FileNotFoundError:
            log.warning("No cache file found at %s", cache_path)

    if not games:
        raise RuntimeError(
            "No data source available. Either configure Firestore credentials "
            f"or ensure cache exists at {cache_path}"
        )

    if apply_deny:
        games = apply_denylist(games)

    # Filter by schema version if requested
    if schema_version is not None:
        before = len(games)
        games = [g for g in games if g.get("schema_version") == schema_version]
        log.info("Filtered to schema_version=%d: %d/%d games", schema_version, len(games), before)

    return games


def load_by_experiment_run_id(run_id: str, cache_path: Path | None = None) -> list[dict]:
    """Load games from a specific experiment run by its UUID.

    Args:
        run_id: The experiment_run_id UUID (can be partial prefix, e.g., first 8 chars)
        cache_path: Optional custom cache file path (defaults to CACHE_PATH)

    Returns:
        List of raw_game dicts from the specified experiment run

    Example:
        # Load by full UUID
        games = load_by_experiment_run_id("a1b2c3d4-5e6f-7g8h-9i0j-k1l2m3n4o5p6")

        # Load by prefix (first 8 chars)
        games = load_by_experiment_run_id("a1b2c3d4")
    """
    if cache_path is None:
        cache_path = CACHE_PATH

    # Try Firestore first with exact filter
    try:
        games = load_from_firestore(filters={"game_config.experiment_run_id": run_id})
        if games:
            return games
    except Exception:
        pass  # Fall back to cache

    # Load from cache and filter client-side
    try:
        all_games = load_from_cache(cache_path)
        # Support partial prefix matching (e.g., first 8 chars of UUID)
        filtered = [
            g for g in all_games
            if (g.get("experiment_run_id") or "").startswith(run_id)
        ]
        if not filtered:
            raise ValueError(f"No games found with experiment_run_id matching '{run_id}'")
        log.info("Loaded %d games for experiment_run_id=%s", len(filtered), run_id)
        return filtered
    except FileNotFoundError:
        raise RuntimeError(f"No cache file found at {cache_path}")


# ---------------------------------------------------------------------------
# DataFrame builders
# ---------------------------------------------------------------------------

def build_round_df(games: list[dict]) -> pd.DataFrame:
    """Build a flat DataFrame with one row per (game, round).

    Uses precomputed stats (V2 schema) when available, otherwise computes
    from transcript (V1 schema) for backward compatibility.
    """
    rows = []
    for g in games:
        cheap_talk_turns = g.get("cheap_talk_turns", 3)
        enable_cheap_talk = g.get("enable_cheap_talk", True)
        model_a, model_b = get_agent_models(g)
        for r in g["rounds"]:
            # Try to use precomputed stats (V2 schema)
            stats = r.get("stats", {})

            if stats:
                # V2: Use precomputed statistics
                num_turns_used = stats["num_turns_used"]
                a_num_msgs = stats["agent_a_msg_count"]
                b_num_msgs = stats["agent_b_msg_count"]
                a_speech_chars = stats["agent_a_char_count"]
                b_speech_chars = stats["agent_b_char_count"]
                total_speech_chars = stats["total_char_count"]
                early_submit = stats["early_submit"]
                num_public_msgs = a_num_msgs + b_num_msgs
            else:
                # V1: Compute from transcript (fallback)
                ct = r.get("cheap_talk_transcript", [])
                public_msgs = [e for e in ct if e.get("type") != "thinking"]

                a_speech = [e for e in public_msgs if e["speaker"] == "agent_a"]
                b_speech = [e for e in public_msgs if e["speaker"] == "agent_b"]

                max_turn = max((e.get("turn", 0) for e in public_msgs), default=0)
                num_turns_used = max_turn + 1

                a_num_msgs = len(a_speech)
                b_num_msgs = len(b_speech)
                a_speech_chars = sum(len(e.get("message", "")) for e in a_speech)
                b_speech_chars = sum(len(e.get("message", "")) for e in b_speech)
                total_speech_chars = sum(len(e.get("message", "")) for e in public_msgs)
                early_submit = num_turns_used < cheap_talk_turns
                num_public_msgs = len(public_msgs)

            a_alloc = r.get("agent_a_allocation", {})
            b_alloc = r.get("agent_b_allocation", {})

            # V5+ oracle stats (per-round for rotating, game-level fallback)
            schema_version = g.get("schema_version", 1)
            round_idx = r["round_number"] - 1
            oracle = get_round_oracle(g, round_idx) if schema_version >= 5 else None

            a_reward = r.get("agent_a_reward", 0)
            b_reward = r.get("agent_b_reward", 0)
            joint_reward = a_reward + b_reward
            oracle_collab_max = oracle.get("collab_max") if oracle else None
            joint_eff = (
                joint_reward / oracle_collab_max
                if oracle_collab_max and oracle_collab_max > 0
                else None
            )

            # V5+ label parsing
            label = g.get("experiment_label", "")
            v5_parsed = parse_v5_label(label) if schema_version >= 5 else {}

            # Per-agent individual efficiency
            oracle_v1 = oracle.get("v1") if oracle else None
            oracle_v2 = oracle.get("v2") if oracle else None
            # Solo efficiency: agent_reward / solo_max (what you'd get alone)
            a_solo_eff = (
                a_reward / oracle_v1 if oracle_v1 and oracle_v1 > 0 else None
            )
            b_solo_eff = (
                b_reward / oracle_v2 if oracle_v2 and oracle_v2 > 0 else None
            )
            # Fair-share efficiency: agent_reward / (collab_max / 2)
            fair_share = oracle_collab_max / 2 if oracle_collab_max and oracle_collab_max > 0 else None
            a_fair_eff = a_reward / fair_share if fair_share else None
            b_fair_eff = b_reward / fair_share if fair_share else None

            # V5+ project run details
            a_proj = r.get("agent_a_project_runs") or {}
            b_proj = r.get("agent_b_project_runs") or {}

            # Baseline check
            enable_cheap_talk = g.get("enable_cheap_talk", g.get("game_config", {}).get("enable_cheap_talk", True))
            is_baseline = not enable_cheap_talk

            # Normalize mode: g["mode"] is incorrectly logged as the model name in V5+.
            # Use parse_v5_label for the authoritative stable/shifting value; fall back
            # to goal_type (also correct) and finally the raw field.
            normalized_mode = (
                v5_parsed.get("mode")
                or g.get("goal_type")
                or g["mode"]
            )
            # Rotation comes from parse_v5_label which is always authoritative.
            normalized_rotation = v5_parsed.get("rotation", "unknown")

            row = {
                "game_id": g["game_id"],
                "experiment_label": label,
                "mode": normalized_mode,
                "goal_type": g.get("goal_type", normalized_mode),
                "thinking_mode": g.get("thinking_mode", "unknown"),
                "is_shifting": normalized_mode == "shifting",
                "is_rotating": normalized_rotation == "rotate",
                "is_baseline": is_baseline,
                "outcome_visibility": g.get("outcome_visibility", "unknown"),
                "condition": g["condition"],
                "has_thinking": g.get("has_thinking", False),
                "num_game_rounds": g["num_rounds"],
                "round_number": r["round_number"],
                "overdrawn": r.get("overdrawn", False),
                "agent_a_reward": a_reward,
                "agent_b_reward": b_reward,
                "joint_reward": joint_reward,
                "agent_a_wood": a_alloc.get("wood", 0),
                "agent_a_stone": a_alloc.get("stone", 0),
                "agent_a_gold": a_alloc.get("gold", 0),
                "agent_b_wood": b_alloc.get("wood", 0),
                "agent_b_stone": b_alloc.get("stone", 0),
                "agent_b_gold": b_alloc.get("gold", 0),
                "total_wood": a_alloc.get("wood", 0) + b_alloc.get("wood", 0),
                "total_stone": a_alloc.get("stone", 0) + b_alloc.get("stone", 0),
                "total_gold": a_alloc.get("gold", 0) + b_alloc.get("gold", 0),
                "num_public_msgs": num_public_msgs,
                "num_turns_used": num_turns_used,
                "cheap_talk_turns": cheap_talk_turns,
                "a_speech_chars": a_speech_chars,
                "b_speech_chars": b_speech_chars,
                "total_speech_chars": total_speech_chars,
                "a_num_msgs": a_num_msgs,
                "b_num_msgs": b_num_msgs,
                "early_submit": early_submit,
                "agent_a_values": json.dumps(g["agent_a_values"]),
                "agent_b_values": json.dumps(g["agent_b_values"]),
                # V5+ fields
                "schema_version": schema_version,
                "model_a": model_a,
                "model_b": model_b,
                "pair": f"{model_a} vs {model_b}" if model_a != model_b else model_a,
                "is_cross_play": model_a != model_b,
                "mc_bucket": v5_parsed.get("mc_bucket"),
                "rotation": normalized_rotation,
                "oracle_collab_max": oracle_collab_max,
                "oracle_v1": oracle_v1,
                "oracle_v2": oracle_v2,
                "oracle_mc_ratio": oracle.get("mc_ratio") if oracle else None,
                "joint_efficiency": joint_eff,
                "agent_a_solo_efficiency": a_solo_eff,
                "agent_b_solo_efficiency": b_solo_eff,
                "agent_a_fair_efficiency": a_fair_eff,
                "agent_b_fair_efficiency": b_fair_eff,
                "agent_a_auto_allocated": a_proj.get("auto_allocated"),
                "agent_b_auto_allocated": b_proj.get("auto_allocated"),
                "swapped": g.get("swapped", False),
                "first_speaker": g.get("first_speaker", 0),
            }
            rows.append(row)
    return pd.DataFrame(rows)


def build_agent_df(round_df: pd.DataFrame) -> pd.DataFrame:
    """Unpivot round_df into agent-level rows (two rows per round).

    Each row represents one agent's performance in one round, with:
    - model: the agent's model name
    - is_shifted: whether this agent had its context reset each round
    - reward: this agent's reward
    - opponent_reward: the other agent's reward
    - fair_efficiency: reward / (collab_max / 2) — how well agent captured its fair share
    - individual_share: reward / joint_reward — fraction of joint reward captured

    Shifting logic:
    - agent_shifting is always [False, True] (agents[0]=stable, agents[1]=shifted)
    - first_speaker remaps: _shifting_a = agent_shifting[first_speaker]
    - swapped=True means first_speaker=1, so agent_a gets agent_shifting[1]=True (shifted)
    """
    rows = []
    for _, r in round_df.iterrows():
        is_shifting_game = r.get("is_shifting", False)
        swapped = r.get("swapped", False)

        # In shifting games: agent_shifting = [False, True]
        # first_speaker=0 (not swapped): agent_a=stable, agent_b=shifted
        # first_speaker=1 (swapped):     agent_a=shifted, agent_b=stable
        a_shifted = is_shifting_game and swapped
        b_shifted = is_shifting_game and not swapped

        fair_share = r["oracle_collab_max"] / 2 if r.get("oracle_collab_max") and r["oracle_collab_max"] > 0 else None
        joint = r.get("joint_reward", 0)

        shared = {
            "game_id": r["game_id"],
            "experiment_label": r.get("experiment_label", ""),
            "round_number": r["round_number"],
            "mode": r.get("mode"),
            "is_shifting": is_shifting_game,
            "is_rotating": r.get("is_rotating", False),
            "is_baseline": r.get("is_baseline", False),
            "is_cross_play": r.get("is_cross_play", False),
            "mc_bucket": r.get("mc_bucket"),
            "rotation": r.get("rotation"),
            "overdrawn": r.get("overdrawn", False),
            "joint_reward": joint,
            "oracle_collab_max": r.get("oracle_collab_max"),
            "joint_efficiency": r.get("joint_efficiency"),
            "schema_version": r.get("schema_version"),
            "swapped": swapped,
        }

        # Agent A row
        a_reward = r.get("agent_a_reward", 0)
        b_reward = r.get("agent_b_reward", 0)
        rows.append({
            **shared,
            "agent_id": "agent_a",
            "model": r.get("model_a", "unknown"),
            "opponent_model": r.get("model_b", "unknown"),
            "is_shifted": a_shifted,
            "reward": a_reward,
            "opponent_reward": b_reward,
            "fair_efficiency": a_reward / fair_share if fair_share else None,
            "individual_share": a_reward / joint if joint > 0 else None,
            "speech_chars": r.get("a_speech_chars", 0),
            "num_msgs": r.get("a_num_msgs", 0),
        })

        # Agent B row
        rows.append({
            **shared,
            "agent_id": "agent_b",
            "model": r.get("model_b", "unknown"),
            "opponent_model": r.get("model_a", "unknown"),
            "is_shifted": b_shifted,
            "reward": b_reward,
            "opponent_reward": a_reward,
            "fair_efficiency": b_reward / fair_share if fair_share else None,
            "individual_share": b_reward / joint if joint > 0 else None,
            "speech_chars": r.get("b_speech_chars", 0),
            "num_msgs": r.get("b_num_msgs", 0),
        })

    return pd.DataFrame(rows)


def build_turn_df(games: list[dict]) -> pd.DataFrame:
    """Build a flat DataFrame with one row per public cheap-talk message."""
    rows = []
    for g in games:
        enable_cheap_talk = g.get("enable_cheap_talk", g.get("game_config", {}).get("enable_cheap_talk", True))
        is_baseline = not enable_cheap_talk
        for r in g["rounds"]:
            ct = r.get("cheap_talk_transcript", [])
            for entry in ct:
                if entry.get("type") == "thinking":
                    continue
                rows.append({
                    "game_id": g["game_id"],
                    "experiment_label": g.get("experiment_label", ""),
                    "mode": g["mode"],
                    "goal_type": g["goal_type"],
                    "thinking_mode": g.get("thinking_mode", "unknown"),
                    "outcome_visibility": g.get("outcome_visibility", "unknown"),
                    "condition": g["condition"],
                    "has_thinking": g.get("has_thinking", False),
                    "is_baseline": is_baseline,
                    "round_number": r["round_number"],
                    "turn_number": entry.get("turn", 0),
                    "speaker": entry["speaker"],
                    "message": entry.get("message", ""),
                    "message_len": len(entry.get("message", "")),
                    "overdrawn": r.get("overdrawn", False),
                    "agent_a_reward": r.get("agent_a_reward", 0),
                    "agent_b_reward": r.get("agent_b_reward", 0),
                })
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Dataset summary
# ---------------------------------------------------------------------------

def filter_games(
    games: list[dict],
    run_ids: list[str] | None = None,
    min_schema_version: int | None = None,
    models: list[str] | None = None,
    modes: list[str] | None = None,
    labels: list[str] | None = None,
) -> list[dict]:
    """Filter games by experiment run ID prefixes, schema version, model, mode, or label.

    Args:
        games: List of raw_game dicts
        run_ids: Experiment run ID prefixes to include (OR logic)
        min_schema_version: Minimum schema version (inclusive)
        models: Model name substrings to include (OR logic, matched against experiment_label)
        modes: Game modes to include ("stable", "shifting")
        labels: Experiment label substrings to include (OR logic)

    Returns:
        Filtered list of raw_game dicts
    """
    filtered = games

    if min_schema_version is not None:
        filtered = [g for g in filtered if g.get("schema_version", 1) >= min_schema_version]

    if run_ids:
        filtered = [
            g for g in filtered
            if any(
                (g.get("experiment_run_id") or "").startswith(rid)
                for rid in run_ids
            )
        ]

    if models:
        filtered = [
            g for g in filtered
            if any(m in g.get("experiment_label", "") for m in models)
            or any(m in (g.get("model_a") or "") for m in models)
            or any(m in (g.get("model_b") or "") for m in models)
        ]

    if modes:
        filtered = [g for g in filtered if g.get("mode") in modes]

    if labels:
        filtered = [
            g for g in filtered
            if any(l in g.get("experiment_label", "") for l in labels)
        ]

    return filtered


def summarize_dataset(games: list[dict]) -> None:
    """Print a summary of the loaded dataset."""
    print(f"Total games: {len(games)}")
    print(f"Experiment labels: {Counter(g.get('experiment_label', '') for g in games)}")
    print(f"Conditions (mode×goal): {Counter(g['condition'] for g in games)}")
    print(f"Modes: {Counter(g['mode'] for g in games)}")
    print(f"Goal types: {Counter(g['goal_type'] for g in games)}")
    print(f"Thinking modes: {Counter(g.get('thinking_mode', 'unknown') for g in games)}")
    print(f"Outcome visibility: {Counter(g.get('outcome_visibility', 'unknown') for g in games)}")
    print(f"Num rounds dist: {Counter(g['num_rounds'] for g in games)}")


# ---------------------------------------------------------------------------
# Shared CLI argument parsing for RQ scripts
# ---------------------------------------------------------------------------

def add_common_args(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Add common data-filtering CLI arguments to an argparse parser.

    Usage in RQ scripts:
        parser = argparse.ArgumentParser(description="RQ1: ...")
        add_common_args(parser)
        # add script-specific args here
        args = parser.parse_args()
        dataset = load_dataset_from_args(args)

    Available filters:
        --schema-version INT       exact schema version
        --run-id STR               experiment run ID prefix
        --mode {stable,shifting}   game mode
        --model STR                model name substring (e.g. haiku, sonnet)
        --model-a STR              filter by agent_a model
        --model-b STR              filter by agent_b model
        --mc-bucket FLOAT          M/C ratio bucket (0.5, 0.8, or 1.0)
        --baseline / --no-baseline include only baseline (no-talk) or only talk games
        --cross-play / --self-play include only cross-play or self-play games
        --rotating / --fixed       include only rotating or fixed-project games
        --share-projects           include only share_projects=True games
        --tom                      include only think_about_opponent=True games
        --label STR                filter by experiment_label substring
        --main-cohort              restrict to the 720-game paper main cohort
    """
    parser.add_argument(
        "--main-cohort", action="store_true", default=False,
        help="Restrict to the 720-game paper main cohort (4 run IDs, excl. tombstoned games)",
    )
    parser.add_argument(
        "--schema-version", type=int, default=None,
        help="Filter to a specific schema version (e.g., 7)",
    )
    parser.add_argument(
        "--run-id", type=str, default=None,
        help="Filter to a specific experiment run ID (prefix match)",
    )
    parser.add_argument(
        "--mode", type=str, default=None, choices=["stable", "shifting"],
        help="Filter to stable or shifting games",
    )
    # Legacy alias
    parser.add_argument(
        "--goal-type", type=str, default=None, choices=["stable", "shifting"],
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--model", type=str, nargs="+", default=None,
        help="Filter to games whose experiment_label contains any of these substrings (OR logic)",
    )
    parser.add_argument(
        "--model-a", type=str, default=None,
        help="Filter to games where agent_a uses this model (exact match)",
    )
    parser.add_argument(
        "--model-b", type=str, default=None,
        help="Filter to games where agent_b uses this model (exact match)",
    )
    parser.add_argument(
        "--mc-bucket", type=float, default=None,
        help="Filter to M/C ratio bucket (0.5=competitive, 0.8=mixed, 1.0=collaborative)",
    )
    baseline_group = parser.add_mutually_exclusive_group()
    baseline_group.add_argument(
        "--baseline", dest="baseline", action="store_true", default=None,
        help="Include only no-talk baseline games",
    )
    baseline_group.add_argument(
        "--no-baseline", dest="baseline", action="store_false",
        help="Exclude no-talk baseline games (default for most analyses)",
    )
    crossplay_group = parser.add_mutually_exclusive_group()
    crossplay_group.add_argument(
        "--cross-play", dest="cross_play", action="store_true", default=None,
        help="Include only cross-play games (model_a != model_b)",
    )
    crossplay_group.add_argument(
        "--self-play", dest="cross_play", action="store_false",
        help="Include only self-play games (model_a == model_b)",
    )
    rotation_group = parser.add_mutually_exclusive_group()
    rotation_group.add_argument(
        "--rotating", dest="rotating", action="store_true", default=None,
        help="Include only rotating-project games",
    )
    rotation_group.add_argument(
        "--fixed", dest="rotating", action="store_false",
        help="Include only fixed-project games",
    )
    share_group = parser.add_mutually_exclusive_group()
    share_group.add_argument(
        "--share-projects", dest="share_projects", action="store_true", default=None,
        help="Include only games with share_projects=True",
    )
    share_group.add_argument(
        "--no-share-projects", dest="share_projects", action="store_false",
        help="Exclude games with share_projects=True",
    )
    tom_group = parser.add_mutually_exclusive_group()
    tom_group.add_argument(
        "--tom", dest="think_about_opponent", action="store_true", default=None,
        help="Include only games with think_about_opponent=True",
    )
    tom_group.add_argument(
        "--no-tom", dest="think_about_opponent", action="store_false",
        help="Exclude games with think_about_opponent=True",
    )
    parser.add_argument(
        "--label", type=str, default=None,
        help="Filter to games whose experiment_label contains this string",
    )
    parser.add_argument(
        "--condition", type=str, default=None,
        help=argparse.SUPPRESS,  # legacy, kept for backward compat
    )
    return parser


def load_games_from_args(args: argparse.Namespace) -> list[dict]:
    """Load and filter games based on parsed CLI arguments from add_common_args().

    Returns a list of raw_game dicts after applying all requested filters.
    """
    if args.run_id:
        games = load_by_experiment_run_id(args.run_id)
    else:
        games = load_experiment_data(schema_version=args.schema_version)

    # Apply additional filters
    if args.goal_type:
        games = [g for g in games if g.get("goal_type") == args.goal_type]
    if args.model:
        games = filter_games(games, models=args.model)
    if args.condition:
        games = [g for g in games if g.get("condition") == args.condition]

    print(f"Loaded {len(games)} games", end="")
    filters_desc = []
    if args.schema_version:
        filters_desc.append(f"schema_version={args.schema_version}")
    if args.run_id:
        filters_desc.append(f"run_id={args.run_id}")
    if args.goal_type:
        filters_desc.append(f"goal_type={args.goal_type}")
    if args.model:
        filters_desc.append(f"model={','.join(args.model)}")
    if args.condition:
        filters_desc.append(f"condition={args.condition}")
    if filters_desc:
        print(f" (filters: {', '.join(filters_desc)})")
    else:
        print()

    return games

def load_dataset_from_args(args: argparse.Namespace) -> 'NegotiationDataset':
    """Load and filter a NegotiationDataset from parsed CLI arguments.

    Applies all filters registered by add_common_args() and prints a summary.
    """
    dataset = load_dataset(
        run_id=getattr(args, "run_id", None),
        schema_version=getattr(args, "schema_version", None),
    )

    games = dataset.games
    filters_applied: list[str] = []

    # Main cohort (--main-cohort)
    if getattr(args, "main_cohort", False):
        games = [
            g for g in games
            if g.config.get("experiment_run_id") in MAIN_COHORT_RUN_IDS
            and not any(g.game_id.startswith(t) for t in TOMBSTONED_GAME_IDS)
        ]
        filters_applied.append("main_cohort=True")

    # Mode (--mode or legacy --goal-type)
    mode = getattr(args, "mode", None) or getattr(args, "goal_type", None)
    if mode:
        games = [g for g in games if g.mode == mode]
        filters_applied.append(f"mode={mode}")

    # Model label substring (--model)
    model = getattr(args, "model", None)
    if model:
        models = [model] if isinstance(model, str) else model
        games = [
            g for g in games
            if any(m in g.label for m in models)
            or any(m in (g.model_a or "") for m in models)
            or any(m in (g.model_b or "") for m in models)
        ]
        filters_applied.append(f"model~{','.join(models)}")

    # Agent-specific model exact match
    model_a = getattr(args, "model_a", None)
    if model_a:
        games = [g for g in games if g.model_a == model_a]
        filters_applied.append(f"model_a={model_a}")

    model_b = getattr(args, "model_b", None)
    if model_b:
        games = [g for g in games if g.model_b == model_b]
        filters_applied.append(f"model_b={model_b}")

    # M/C bucket
    mc_bucket = getattr(args, "mc_bucket", None)
    if mc_bucket is not None:
        games = [g for g in games if g.metadata.get("mc_bucket") == mc_bucket]
        filters_applied.append(f"mc_bucket={mc_bucket}")

    # Baseline flag
    baseline = getattr(args, "baseline", None)
    if baseline is True:
        games = [g for g in games if g.is_baseline]
        filters_applied.append("baseline=True")
    elif baseline is False:
        games = [g for g in games if not g.is_baseline]
        filters_applied.append("baseline=False")

    # Cross-play flag
    cross_play = getattr(args, "cross_play", None)
    if cross_play is True:
        games = [g for g in games if g.is_cross_play]
        filters_applied.append("cross_play=True")
    elif cross_play is False:
        games = [g for g in games if not g.is_cross_play]
        filters_applied.append("self_play=True")

    # Rotation flag
    rotating = getattr(args, "rotating", None)
    if rotating is True:
        games = [g for g in games if g.is_rotating]
        filters_applied.append("rotating=True")
    elif rotating is False:
        games = [g for g in games if not g.is_rotating]
        filters_applied.append("rotating=False")

    # Share projects
    share_projects = getattr(args, "share_projects", None)
    if share_projects is True:
        games = [g for g in games if g.metadata.get("share_projects")]
        filters_applied.append("share_projects=True")
    elif share_projects is False:
        games = [g for g in games if not g.metadata.get("share_projects")]
        filters_applied.append("share_projects=False")

    # Think about opponent
    tom = getattr(args, "think_about_opponent", None)
    if tom is True:
        games = [g for g in games if g.metadata.get("think_about_opponent")]
        filters_applied.append("think_about_opponent=True")
    elif tom is False:
        games = [g for g in games if not g.metadata.get("think_about_opponent")]
        filters_applied.append("think_about_opponent=False")

    # Label substring
    label = getattr(args, "label", None)
    if label:
        games = [g for g in games if label in g.label]
        filters_applied.append(f"label~{label}")

    filters_str = f" (filters: {', '.join(filters_applied)})" if filters_applied else ""
    print(f"Loaded {len(games)} games{filters_str}")

    return NegotiationDataset(games)


def load_dataset(
    filters: dict | None = None,
    run_id: str | None = None,
    schema_version: int | None = None,
    apply_deny: bool = True,
) -> NegotiationDataset:
    """Load experiment data as a NegotiationDataset (structured models)."""
    if run_id:
        games = load_by_experiment_run_id(run_id)
    else:
        # We need raw traces here. load_experiment_data currently returns converted dicts.
        # Let's use load_from_cache or load_from_firestore directly to get raw traces.
        try:
            games = load_from_cache()
        except Exception:
            games = load_from_firestore(filters=filters)
    
    if apply_deny:
        games = apply_denylist(games)
        
    if schema_version:
        games = [g for g in games if g.get("schema_version") == schema_version]

    # Filter out games without game_config (standardized dicts) if we want pure raw traces,
    # but our NegotiationGame model is now robust enough to handle both.
    return NegotiationDataset.from_traces(games)
