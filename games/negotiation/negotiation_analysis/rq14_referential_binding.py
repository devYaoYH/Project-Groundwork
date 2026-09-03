"""RQ14: Referential Binding Failures in Negotiation

Detects instances where one agent introduces a named plan/option/proposal
and the other agent's response (speech or thinking) reveals they do not
understand what concrete resource purchases that label maps to.

Two-stage pipeline:
1. **Regex extraction** (fast, free) — extract candidate episodes where:
   - A message contains a named reference (project name, option label, plan)
   - A subsequent message from the other agent exists
2. **LLM-as-judge** (celled, cached) — classify each episode as:
   - `project_mismatch` — speaker describes project requirements differing from responder's info
   - `confusion` — responder expresses uncertainty about what the plan means
   - `blind_acceptance` — responder agrees without verifying concrete allocations
   - `grounded` — responder demonstrates understanding (no failure)

Usage:
    uv run python -m scripts.analysis.rq14_referential_binding
    uv run python -m scripts.analysis.rq14_referential_binding --candidates-only
    uv run python -m scripts.analysis.rq14_referential_binding --no-cache
"""

import argparse
import json
import re
from pathlib import Path

import pandas as pd

from negotiation_game.backend.agents.api import call_llm_oneshot
from negotiation_game.backend.agents.factory import detect_provider, get_api_key_for_provider
from negotiation_game.backend.defaults import LLM_PROVIDERS
from negotiation_analysis.data_loader import add_common_args, load_dataset_from_args
from negotiation_analysis.models import NegotiationDataset
CACHE_PATH = Path("data/rq14_judgments.json")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Pattern libraries
# ---------------------------------------------------------------------------

def _has_plan_reference_or_purchase(msg: str, resource_names: list[str]) -> bool:
    """Check if message contains plan reference OR explicit purchase statement.

    Plan references: "I choose option A", "let's go with plan B", "doing scenario C"
    Explicit purchases: "I'll buy 5 wood and 3 stone", "taking 4 gold"
    """
    # Named plan references: "option A", "plan B", "scenario C", etc.
    plan_ref = r"(?:I\s+(?:choose|pick|will\s+(?:go\s+with|do|take)|am\s+(?:going\s+with|doing)))\s+(?:option|plan|scenario|approach)\s+[A-C1-3]"
    plan_ref += r"|(?:(?:let.s|I.ll|I\s+will)\s+(?:go\s+with|do|stick\s+with|take))\s+(?:option|plan|scenario|approach)?\s*[A-C1-3]"
    plan_ref += r"|(?:(?:choosing|picking|going\s+with|doing)\s+(?:option|plan|scenario)\s+[A-C1-3])"

    if re.search(plan_ref, msg, re.I):
        return True

    # Purchase verbs with quantities
    purchase_verbs = r"(?:I(?:'ll|.ll|\s+will|\s+am)?\s+(?:buy|take|purchase|get|going\s+(?:to\s+)?(?:buy|take|get)))"
    purchase_verbs += r"|(?:buying|taking|purchasing|getting)"
    purchase_verbs += r"|(?:I(?:'m|.m)?\s+(?:buying|taking|getting))"

    if not re.search(purchase_verbs, msg, re.I):
        return False

    # Check for at least one resource with quantity
    resource_pattern = r"(\d+)\s*(?:units?\s+(?:of\s+)?)?(" + "|".join(re.escape(r) for r in resource_names) + r")"
    return bool(re.search(resource_pattern, msg, re.I))


def _get_agent_last_messages(transcript: list[dict], agent_id: str) -> tuple[str, str]:
    """Get agent's last speech and thinking messages (may be empty strings)."""
    agent_msgs = [e for e in transcript if e.get("speaker") == agent_id]
    if not agent_msgs:
        return "", ""

    # Find last speech
    speech_msgs = [e for e in agent_msgs if e.get("type", "speech") == "speech"]
    last_speech = speech_msgs[-1].get("message", "") if speech_msgs else ""

    # Find last thinking (look for it near the last speech)
    last_thinking = ""
    if speech_msgs:
        last_speech_turn = speech_msgs[-1].get("turn", -1)
        # Get thinking from same turn or just before
        thinking_msgs = [
            e for e in agent_msgs
            if e.get("type") == "thinking" and abs(e.get("turn", -1) - last_speech_turn) <= 1
        ]
        if thinking_msgs:
            last_thinking = thinking_msgs[-1].get("message", "")

    return last_speech, last_thinking


# ---------------------------------------------------------------------------
# Stage 1: Candidate extraction (regex-based)
# ---------------------------------------------------------------------------

def _extract_candidates_in_round(
    episode_uid: str,
    round_number: int,
    transcript: list[dict],
    a_alloc: dict,
    b_alloc: dict,
    a_projects: list[dict],
    b_projects: list[dict],
    model_a: str,
    model_b: str,
    game_meta: dict,
    resource_names: list[str],
) -> list[dict]:
    """Extract candidates from last turn of each round.

    Logic:
    1. Get each agent's last speech and thinking messages
    2. Determine who spoke last (first agent to finalize)
    3. Filter: first agent's speech must explicitly state purchase with quantities
    4. Pass both agents' speech + thinking to classifier
    """
    # Get last messages from each agent
    a_speech, a_thinking = _get_agent_last_messages(transcript, "agent_a")
    b_speech, b_thinking = _get_agent_last_messages(transcript, "agent_b")

    if not a_speech or not b_speech:
        return []

    # Build project-requirement maps
    proj_reqs: dict[str, dict[str, set[str]]] = {"agent_a": {}, "agent_b": {}}
    for proj in (a_projects or []):
        proj_reqs["agent_a"][proj["name"]] = set(proj.get("requirements", {}).keys())
    for proj in (b_projects or []):
        proj_reqs["agent_b"][proj["name"]] = set(proj.get("requirements", {}).keys())

    # Determine who spoke last (to identify first agent)
    agent_a_msgs = [e for e in transcript if e.get("speaker") == "agent_a"]
    agent_b_msgs = [e for e in transcript if e.get("speaker") == "agent_b"]

    if not agent_a_msgs or not agent_b_msgs:
        return []

    a_last_turn = max(e.get("turn", -1) for e in agent_a_msgs)
    b_last_turn = max(e.get("turn", -1) for e in agent_b_msgs)

    # First agent = one who spoke first in final exchange
    if a_last_turn < b_last_turn:
        first_agent, second_agent = "agent_a", "agent_b"
        first_speech, first_thinking = a_speech, a_thinking
        second_speech, second_thinking = b_speech, b_thinking
        first_model, second_model = model_a, model_b
        first_alloc, second_alloc = a_alloc, b_alloc
        first_projects, second_projects = proj_reqs["agent_a"], proj_reqs["agent_b"]
    else:
        first_agent, second_agent = "agent_b", "agent_a"
        first_speech, first_thinking = b_speech, b_thinking
        second_speech, second_thinking = a_speech, a_thinking
        first_model, second_model = model_b, model_a
        first_alloc, second_alloc = b_alloc, a_alloc
        first_projects, second_projects = proj_reqs["agent_b"], proj_reqs["agent_a"]

    # Regex filter: first agent's speech must reference a plan OR state explicit purchase
    if not _has_plan_reference_or_purchase(first_speech, resource_names):
        return []

    # Create candidate with both agents' speech + thinking
    return [{
        "episode_uid": episode_uid,
        "round_number": round_number,
        "first_agent": first_agent,
        "first_agent_model": first_model,
        "first_agent_speech": first_speech,
        "first_agent_thinking": first_thinking,
        "first_agent_allocation": first_alloc,
        "first_agent_projects": first_projects,
        "second_agent": second_agent,
        "second_agent_model": second_model,
        "second_agent_speech": second_speech,
        "second_agent_thinking": second_thinking,
        "second_agent_allocation": second_alloc,
        "second_agent_projects": second_projects,
        **game_meta,
    }]


# ---------------------------------------------------------------------------
# Stage 2: LLM-as-judge classification
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a negotiation analyst classifying referential binding failures in agent-to-agent negotiations.

You will receive episodes where one agent introduces a named reference (like "Option A", "my plan", "project_b") and another agent responds. Your task is to classify whether the responder's message reveals a **referential binding failure** — that is, they fail to ground the reference to the correct concrete resource allocation.

Classification labels:
1. **project_mismatch** — The speaker describes project requirements that differ from what the responder knows. For example, speaker says "project_b needs wood+stone" but responder's private info says project_b needs gold+stone.
2. **confusion** — The responder explicitly expresses uncertainty about what the reference means ("not sure what", "unclear", "don't understand").
3. **blind_acceptance** — The responder agrees to a labelled plan without evidence (in their message) of understanding the concrete allocations. Only applies to speech, not thinking.
4. **grounded** — The responder demonstrates understanding of the concrete allocations (no failure).

For each episode, respond with JSON:
{
  "episode_id": int,
  "failure_type": "project_mismatch" | "confusion" | "blind_acceptance" | "grounded",
  "confidence": 1-5 (1=very uncertain, 5=very confident),
  "reasoning": "brief explanation (1-2 sentences)"
}

Return a JSON array of judgments, one per episode."""


def _format_episode(candidate: dict, episode_id: int) -> str:
    """Format a candidate episode for the LLM judge."""
    first_projects = candidate.get("first_agent_projects", {})
    second_projects = candidate.get("second_agent_projects", {})

    proj_str_first = ", ".join(
        f"{name}: {list(reqs)}" for name, reqs in first_projects.items()
    ) or "none"
    proj_str_second = ", ".join(
        f"{name}: {list(reqs)}" for name, reqs in second_projects.items()
    ) or "none"

    # Format messages with both speech and thinking
    first_msg = f"Speech: \"{candidate['first_agent_speech']}\""
    if candidate.get('first_agent_thinking'):
        first_msg += f"\n  Thinking: \"{candidate['first_agent_thinking']}\""

    second_msg = f"Speech: \"{candidate['second_agent_speech']}\""
    if candidate.get('second_agent_thinking'):
        second_msg += f"\n  Thinking: \"{candidate['second_agent_thinking']}\""

    return f"""
## Episode {episode_id}
**First Agent** ({candidate['first_agent']}):
  {first_msg}

**Second Agent** ({candidate['second_agent']}):
  {second_msg}

**Context**:
- First agent's projects: {proj_str_first}
- Second agent's projects: {proj_str_second}
- First agent's allocation: {candidate.get('first_agent_allocation', {})}
- Second agent's allocation: {candidate.get('second_agent_allocation', {})}
"""


def _judge_episodes_cell(
    candidates: list[dict],
    start_idx: int,
    judge_model: str = "claude-3-haiku-20240307",
) -> dict[str, dict]:
    """Send a cell of episodes to LLM judge and return judgments.

    Returns:
        Dict mapping cache_key -> judgment dict
    """
    provider = detect_provider(judge_model)
    if not provider:
        raise ValueError(f"Could not detect provider for model: {judge_model}")

    api_key = get_api_key_for_provider(provider)
    if not api_key:
        raise ValueError(f"No API key found for provider {provider}. Set {provider.upper()}_API_KEY env var.")

    api_config = LLM_PROVIDERS[provider]

    # Format cell of episodes
    episodes_text = "\n".join(
        _format_episode(c, start_idx + i)
        for i, c in enumerate(candidates)
    )

    user_prompt = f"""Classify the following {len(candidates)} episodes. Return a JSON array of judgments.

{episodes_text}
"""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    try:
        response_text = call_llm_oneshot(
            api_format=api_config["api_format"],
            api_base=api_config["api_base"],
            api_key=api_key,
            model=judge_model,
            messages=messages,
            max_tokens=4096,
            temperature=0.3,
            timeout=120,
        )

        # Extract JSON from response (handle preamble text and markdown)
        json_text = response_text.strip()

        # Handle markdown code blocks
        if "```" in json_text:
            start = json_text.find("```")
            end = json_text.rfind("```")
            if start != -1 and end != -1 and start < end:
                json_text = json_text[start+3:end].strip()
                # Remove language identifier (e.g., "json")
                if json_text.startswith(("json", "JSON")):
                    json_text = json_text[4:].strip()

        # Extract JSON array/object (skip preamble text)
        json_start = -1
        json_end = -1
        for i, char in enumerate(json_text):
            if char in "[{":
                json_start = i
                break
        for i in range(len(json_text) - 1, -1, -1):
            if json_text[i] in "]}":
                json_end = i + 1
                break

        if json_start != -1 and json_end != -1:
            json_text = json_text[json_start:json_end]

        judgments = json.loads(json_text)

        # Map back to candidates by episode_id
        result = {}
        for judgment in judgments:
            idx = judgment["episode_id"] - start_idx
            if 0 <= idx < len(candidates):
                candidate = candidates[idx]
                cache_key = _make_cache_key(candidate)
                result[cache_key] = judgment

        return result

    except Exception as e:
        print(f"WARNING: LLM judge failed for cell starting at {start_idx}: {e}")
        return {}


def _make_cache_key(candidate: dict) -> str:
    """Create a unique cache key for a candidate episode."""
    return f"{candidate['episode_uid']}_{candidate['round_number']}"


def _load_cache() -> dict[str, dict]:
    """Load judgment cache from disk."""
    if not CACHE_PATH.exists():
        return {}
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except Exception as e:
        print(f"WARNING: Could not load cache from {CACHE_PATH}: {e}")
        return {}


def _save_cache(cache: dict[str, dict]) -> None:
    """Save judgment cache to disk."""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_PATH, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"WARNING: Could not save cache to {CACHE_PATH}: {e}")


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def extract_candidates(dataset: NegotiationDataset) -> list[dict]:
    """Stage 1: Extract candidate episodes from all games (regex-based)."""
    all_candidates = []

    for g in dataset.games:
        agent_projects = g.config.get("agent_projects")
        if not agent_projects:
            continue

        resource_names = g.config.get("resource_types", ["wood", "stone", "gold"])

        a_projects = agent_projects[0] if len(agent_projects) > 0 else []
        b_projects = agent_projects[1] if len(agent_projects) > 1 else []

        game_meta = {
            "mode": g.mode,
            "experiment_label": g.label,
        }

        for r in g.rounds:
            ct = r.cheap_talk_transcript
            if not ct:
                continue

            candidates = _extract_candidates_in_round(
                episode_uid=g.episode_uid,
                round_number=r.round_number,
                transcript=ct,
                a_alloc=r.agent_a_resources,
                b_alloc=r.agent_b_resources,
                a_projects=a_projects,
                b_projects=b_projects,
                model_a=g.model_a,
                model_b=g.model_b,
                game_meta=game_meta,
                resource_names=resource_names,
            )
            all_candidates.extend(candidates)

    return all_candidates


def analyze_referential_binding(
    dataset: NegotiationDataset,
    use_llm_judge: bool = True,
    judge_model: str = "claude-3-haiku-20240307",
    cell_size: int = 15,
    use_cache: bool = True,
) -> dict:
    """Two-stage pipeline: extract candidates (regex) → classify (LLM judge).

    Args:
        raw_games: List of environment documents from cache
        use_llm_judge: If False, return only candidates without LLM classification
        judge_model: Model to use for LLM-as-judge (default: claude-3-haiku-20240307)
        cell_size: Number of episodes per LLM call (default: 15)
        use_cache: If True, load cached judgments and skip re-judging (default: True)

    Returns:
        Dict with keys: candidates (list), judgments (dict), episode_df (DataFrame), summary (dict)
    """
    # Stage 1: Extract candidates (fast, regex-based)
    print("Stage 1: Extracting candidate episodes...")
    candidates = extract_candidates(dataset)
    print(f"  Found {len(candidates)} candidate episodes")

    if not use_llm_judge:
        n_project_games = sum(1 for g in dataset.games if g.config.get("agent_projects"))
        n_rounds_with_ct = sum(
            1 for g in dataset.games if g.config.get("agent_projects")
            for r in g.rounds
            if r.cheap_talk_transcript
        )
        return {
            "candidates": candidates,
            "judgments": {},
            "episode_df": pd.DataFrame(),
            "summary": {
                "n_project_games": n_project_games,
                "n_rounds_with_cheap_talk": n_rounds_with_ct,
                "n_candidates": len(candidates),
            },
        }

    # Stage 2: LLM-as-judge classification (with caching)
    print(f"Stage 2: Classifying with LLM judge ({judge_model})...")

    # Load cache
    cache = _load_cache() if use_cache else {}
    print(f"  Loaded {len(cache)} cached judgments")

    # Identify uncached candidates
    uncached = []
    for c in candidates:
        cache_key = _make_cache_key(c)
        if cache_key not in cache:
            uncached.append(c)

    if uncached:
        print(f"  Judging {len(uncached)} new episodes ({len(candidates) - len(uncached)} from cache)...")

        # Cell judge
        for i in range(0, len(uncached), cell_size):
            cell = uncached[i:i + cell_size]
            print(f"    Cell {i // cell_size + 1}/{(len(uncached) + cell_size - 1) // cell_size} ({len(cell)} episodes)...")
            judgments = _judge_episodes_cell(cell, i, judge_model)
            cache.update(judgments)

        # Save updated cache
        if use_cache:
            _save_cache(cache)
            print(f"  Saved {len(cache)} judgments to cache")
    else:
        print(f"  All episodes already cached, no new API calls needed")

    # Merge judgments back into candidates
    episodes = []
    for c in candidates:
        cache_key = _make_cache_key(c)
        judgment = cache.get(cache_key)
        if judgment:
            episodes.append({
                **c,
                "failure_type": judgment.get("failure_type", "unknown"),
                "confidence": judgment.get("confidence", 0),
                "reasoning": judgment.get("reasoning", ""),
            })

    episode_df = pd.DataFrame(episodes)
    summary = _compute_summary(episode_df, dataset, candidates)

    return {
        "candidates": candidates,
        "judgments": cache,
        "episode_df": episode_df,
        "summary": summary,
    }


def _compute_summary(df: pd.DataFrame, dataset: NegotiationDataset, candidates: list[dict]) -> dict:
    n_project_games = sum(1 for g in dataset.games if g.config.get("agent_projects"))
    n_rounds_with_ct = sum(
        1 for g in dataset.games if g.config.get("agent_projects")
        for r in g.rounds
        if r.cheap_talk_transcript
    )

    summary = {
        "n_project_games": n_project_games,
        "n_rounds_with_cheap_talk": n_rounds_with_ct,
        "n_candidates": len(candidates),
    }

    if df.empty:
        summary["n_classified"] = 0
        return summary

    n = len(df)
    summary["n_classified"] = n

    # Filter to actual failures (not "grounded")
    failures_df = df[df["failure_type"] != "grounded"]
    n_failures = len(failures_df)

    summary["n_failures"] = n_failures
    summary["n_unique_games_with_failures"] = failures_df["episode_uid"].nunique() if n_failures > 0 else 0
    summary["n_unique_rounds_with_failures"] = (
        failures_df.groupby(["episode_uid", "round_number"]).ngroups if n_failures > 0 else 0
    )

    # By failure type
    summary["by_failure_type"] = df["failure_type"].value_counts().to_dict()

    # By confidence
    if "confidence" in df.columns:
        summary["avg_confidence"] = df["confidence"].mean()
        summary["by_confidence"] = df["confidence"].value_counts().sort_index().to_dict()

    # By model (second agent, who responds to first agent's purchase statement)
    if n_failures > 0:
        summary["by_second_agent_model"] = (
            failures_df.groupby("second_agent_model")["failure_type"]
            .value_counts()
            .unstack(fill_value=0)
            .to_dict()
        )

    # Rates
    summary["failure_rate"] = n_failures / n if n > 0 else 0
    summary["failures_per_round"] = n_failures / n_rounds_with_ct if n_rounds_with_ct else 0
    summary["games_with_failures_rate"] = (
        failures_df["episode_uid"].nunique() / n_project_games if n_project_games and n_failures > 0 else 0
    )

    return summary


# ---------------------------------------------------------------------------
# Printing
# ---------------------------------------------------------------------------

def print_summary(results: dict, show_examples: bool = True) -> None:
    print("=" * 70)
    print("RQ14: Referential Binding Failures (LLM-as-Judge)")
    print("=" * 70)

    s = results["summary"]
    df = results["episode_df"]

    print(f"\nProject games analysed: {s['n_project_games']}")
    print(f"Rounds with cheap talk: {s['n_rounds_with_cheap_talk']}")
    print(f"Candidate episodes extracted: {s['n_candidates']}")

    if s.get("n_classified", 0) == 0:
        print("\nNo episodes classified (use without --candidates-only to run LLM judge).")
        return

    print(f"Episodes classified: {s['n_classified']}")
    print(f"Binding failures detected: {s['n_failures']} ({s['failure_rate']:.1%} of classified)")

    if s["n_failures"] == 0:
        print("\nNo binding failures found in classified episodes.")
        return

    print(f"\nUnique games with failures: {s['n_unique_games_with_failures']} ({s['games_with_failures_rate']:.0%} of project games)")
    print(f"Failures per cheap-talk round: {s['failures_per_round']:.2f}")

    if "avg_confidence" in s:
        print(f"\nAverage confidence: {s['avg_confidence']:.2f}/5")
        if "by_confidence" in s:
            print("Confidence distribution:")
            for conf, count in sorted(s["by_confidence"].items()):
                print(f"  {conf}: {count}")

    print(f"\nBy failure type:")
    for ftype, count in sorted(s["by_failure_type"].items(), key=lambda x: -x[1]):
        pct = 100 * count / s["n_classified"] if s["n_classified"] > 0 else 0
        print(f"  {ftype}: {count} ({pct:.1f}%)")

    if "by_second_agent_model" in s and s["by_second_agent_model"]:
        print(f"\nBy second agent model (failures only):")
        model_df = pd.DataFrame(s["by_second_agent_model"]).T.fillna(0).astype(int)
        model_df["total"] = model_df.sum(axis=1)
        print(model_df.sort_values("total", ascending=False).to_string())

    # List games with failures
    if s["n_failures"] > 0:
        failures_df = df[df["failure_type"] != "grounded"]
        games_with_failures = failures_df.groupby("episode_uid").agg({
            "failure_type": ["count", lambda x: ", ".join(sorted(set(x)))],
        })
        games_with_failures.columns = ["n_failures", "failure_types"]
        games_with_failures = games_with_failures.sort_values("n_failures", ascending=False)

        print(f"\nGames with failures ({len(games_with_failures)} games):")
        print("=" * 70)
        for episode_uid, row in games_with_failures.head(20).iterrows():
            print(f"  {episode_uid}: {row['n_failures']} failure{'s' if row['n_failures'] > 1 else ''} ({row['failure_types']})")
        if len(games_with_failures) > 20:
            print(f"  ... and {len(games_with_failures) - 20} more games")

    # Print examples
    if show_examples and not df.empty:
        print(f"\n{'='*70}")
        print("Sample Failure Episodes (up to 5 per type)")
        print(f"{'='*70}")
        for ftype in ["confusion", "project_mismatch", "blind_acceptance"]:
            subset = df[df["failure_type"] == ftype]
            if subset.empty:
                continue
            print(f"\n--- {ftype.upper()} ({len(subset)} total) ---")
            for i, (_, row) in enumerate(subset.head(5).iterrows(), 1):
                print(f"\n[{i}] Environment {row['episode_uid']}, Round {row['round_number']} (confidence: {row.get('confidence', 0)}/5)")
                print(f"    First agent ({row['first_agent']}, {row['first_agent_model']}):")
                print(f"      Speech: \"{row['first_agent_speech'][:300]}{'...' if len(row['first_agent_speech']) > 300 else ''}\"")
                if row.get('first_agent_thinking'):
                    print(f"      Thinking: \"{row['first_agent_thinking'][:200]}{'...' if len(row['first_agent_thinking']) > 200 else ''}\"")
                print(f"    Second agent ({row['second_agent']}, {row['second_agent_model']}):")
                print(f"      Speech: \"{row['second_agent_speech'][:300]}{'...' if len(row['second_agent_speech']) > 300 else ''}\"")
                if row.get('second_agent_thinking'):
                    print(f"      Thinking: \"{row['second_agent_thinking'][:200]}{'...' if len(row['second_agent_thinking']) > 200 else ''}\"")
                if "reasoning" in row and row["reasoning"]:
                    print(f"    Judge reasoning: {row['reasoning']}")


def export_candidates_to_markdown(candidates: list[dict], output_path: Path) -> None:
    """Export candidates to a markdown file for easy viewing."""
    with open(output_path, 'w') as f:
        f.write("# RQ14: Referential Binding Candidates\n\n")
        f.write(f"**Total candidates extracted:** {len(candidates)}\n\n")
        f.write("Each candidate represents the final turn of a round where the first agent references a plan or states a purchase.\n\n")
        f.write("---\n\n")

        for i, c in enumerate(candidates, 1):
            f.write(f"## Candidate {i}\n\n")
            f.write(f"**Environment:** `{c['episode_uid']}`  \n")
            f.write(f"**Round:** {c['round_number']}  \n")
            f.write(f"**Mode:** {c.get('mode', 'N/A')} | **Goal:** {c.get('goal_type', 'N/A')}  \n")
            f.write(f"**Experiment Label:** {c.get('experiment_label', 'N/A')}  \n\n")

            # First agent
            f.write(f"### First Agent ({c['first_agent']}, {c['first_agent_model']})\n\n")
            f.write(f"**Speech:**\n```\n{c['first_agent_speech']}\n```\n\n")
            if c.get('first_agent_thinking'):
                f.write(f"**Thinking:**\n```\n{c['first_agent_thinking']}\n```\n\n")
            f.write(f"**Allocation:** {c.get('first_agent_allocation', {})}  \n")
            f.write(f"**Projects:** {dict(c.get('first_agent_projects', {}))}  \n\n")

            # Second agent
            f.write(f"### Second Agent ({c['second_agent']}, {c['second_agent_model']})\n\n")
            f.write(f"**Speech:**\n```\n{c['second_agent_speech']}\n```\n\n")
            if c.get('second_agent_thinking'):
                f.write(f"**Thinking:**\n```\n{c['second_agent_thinking']}\n```\n\n")
            f.write(f"**Allocation:** {c.get('second_agent_allocation', {})}  \n")
            f.write(f"**Projects:** {dict(c.get('second_agent_projects', {}))}  \n\n")

            f.write("---\n\n")

    print(f"\nExported {len(candidates)} candidates to: {output_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="RQ14: Referential Binding Failures (Two-stage: Regex + LLM-as-Judge)"
    )
    add_common_args(parser)
    parser.add_argument(
        "--candidates-only",
        action="store_true",
        help="Only extract candidates without LLM classification",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Ignore cached judgments and re-judge all episodes",
    )
    parser.add_argument(
        "--judge-model",
        default="claude-3-haiku-20240307",
        help="Model to use for LLM-as-judge (default: claude-3-haiku-20240307)",
    )
    parser.add_argument(
        "--cell-size",
        type=int,
        default=15,
        help="Number of episodes per LLM call (default: 15)",
    )
    parser.add_argument(
        "--export-candidates",
        type=str,
        help="Export candidates to markdown file (e.g., data/rq14_candidates.md)",
    )
    args = parser.parse_args()

    dataset = load_dataset_from_args(args)

    results = analyze_referential_binding(
        dataset,
        use_llm_judge=not args.candidates_only,
        judge_model=args.judge_model,
        cell_size=args.cell_size,
        use_cache=not args.no_cache,
    )

    if args.export_candidates:
        export_candidates_to_markdown(results["candidates"], Path(args.export_candidates))

    print_summary(results)
