"""RQ6: Do Agents' Stated Orders Cohere with Final Decisions?

Extract resource intentions from cheap talk and compare with actual allocations.
When they diverge, analyze why.

Also computes project mention frequency — how often agents reference specific
projects (project_a, project_b, etc.) to signal intentions during cheap talk.

Usage:
    uv run python -m scripts.analysis.rq6_stated_vs_actual
"""

import json
import argparse
import re

import numpy as np
import pandas as pd

from negotiation_analysis.data_loader import add_common_args, load_dataset_from_args, RESOURCES
from negotiation_analysis.models import NegotiationDataset


def extract_intentions(
    message: str, resource_types: list[str] | None = None,
) -> dict[str, int]:
    """Extract stated resource intentions from a message.

    Args:
        message: The chat message to parse.
        resource_types: List of resource names for this game (e.g. ["pixie_dust",
            "moonstone", "ruby"]). Falls back to default RESOURCES if not provided.

    Returns a dict mapping resource -> quantity (or -1 for focus without qty).
    """
    resources = resource_types or RESOURCES
    msg = str(message).lower()

    # Explicit DECISION tag
    decision_match = re.search(r"\[decision\]\s*(\{.*?\})", msg, re.IGNORECASE)
    if decision_match:
        try:
            parsed = json.loads(decision_match.group(1).replace("'", '"'))
            return {r: parsed[r] for r in resources if r in parsed}
        except (json.JSONDecodeError, AttributeError):
            pass

    intentions: dict[str, int] = {}

    # Specific quantities: "5 wood", "wood: 5", "buy 3 gold"
    # Use word boundaries to avoid substring matches (e.g. "stone" in "moonstone")
    for r in resources:
        r_lower = r.lower()
        # For multi-word resources (pixie_dust), also match "pixie dust"
        r_pattern = re.escape(r_lower).replace(r"\_", r"[_ ]")
        qty_match = re.search(
            r"(\d+)\s+(?:units?\s+(?:of\s+)?)?" + r_pattern + r"\b"
            + r"|(?<![a-z])" + r_pattern + r"\s*[:\-]?\s*(\d+)",
            msg,
        )
        if qty_match:
            qty = int(qty_match.group(1) or qty_match.group(2))
            intentions[r] = qty

    # General resource focus (if no quantities found)
    if not intentions:
        for r in resources:
            r_lower = r.lower()
            r_pattern = re.escape(r_lower).replace(r"\_", r"[_ ]")
            focus_patterns = [
                r"(?:i'll|i will|i'm|going to|my|i)\s+"
                r"(?:focus|go|take|claim|buy|get|pick|grab|prioriti[sz]e|speciali[sz]e)"
                r"(?:\s+(?:on|in|with))?\s+(?:the\s+)?" + r_pattern + r"\b",
                r"my\s+(?:priority|focus|top|main)\s+(?:is|will be)\s+" + r_pattern + r"\b",
            ]
            for pat in focus_patterns:
                if re.search(pat, msg):
                    intentions[r] = -1
                    break

    return intentions


def extract_project_mentions(
    message: str, project_names: list[str] | None = None,
) -> dict[str, int]:
    """Extract project mentions from a message.

    Args:
        message: The chat message to parse.
        project_names: List of project names (e.g. ["project_a", "project_b"]).
            If None, detects any project_X pattern.

    Returns dict mapping project_name -> mention count in this message.
    """
    msg = str(message).lower()
    mentions: dict[str, int] = {}

    if project_names:
        for p in project_names:
            p_lower = p.lower()
            p_pattern = re.escape(p_lower).replace(r"\_", r"[_ ]")
            count = len(re.findall(p_pattern, msg))
            if count > 0:
                mentions[p] = count
    else:
        # Auto-detect project_X patterns
        for m in re.finditer(r"project[_ ]([a-z])\b", msg):
            name = f"project_{m.group(1)}"
            mentions[name] = mentions.get(name, 0) + 1

    return mentions


def analyze_stated_vs_actual(raw_games: list[dict]) -> dict:
    """Compare stated intentions with actual allocations.

    Uses each game's resource_types (for V5+ themed resources) instead of
    hardcoded defaults. Also computes project mention frequency.
    """
    records = []
    project_records = []
    divergence_examples = []

    for g in raw_games:
        resource_types = g.get("resource_types", RESOURCES)
        # Collect project names from game config
        projects = g.get("game_config", {}).get("projects") or g.get("projects")
        project_names = list(projects.keys()) if isinstance(projects, dict) else None

        for r in g["rounds"]:
            ct = r.get("cheap_talk_transcript", [])
            public_msgs = [e for e in ct if e.get("type") != "thinking"]

            for agent in ["agent_a", "agent_b"]:
                agent_msgs = [
                    e for e in public_msgs if e["speaker"] == agent
                ]

                # --- Resource intention extraction ---
                all_intentions: dict[str, int] = {}
                for m in agent_msgs:
                    intents = extract_intentions(
                        m.get("message", ""), resource_types=resource_types,
                    )
                    all_intentions.update(intents)

                if all_intentions:
                    actual = r.get(f"{agent}_allocation", {})
                    stated_resources = set(all_intentions.keys())
                    actual_resources = {
                        res for res, q in actual.items() if q > 0
                    }

                    resource_overlap = (
                        len(stated_resources & actual_resources)
                        / max(len(stated_resources | actual_resources), 1)
                    )
                    resource_match = stated_resources == actual_resources

                    qty_matches = 0
                    qty_total = 0
                    for res, qty in all_intentions.items():
                        if qty > 0:
                            qty_total += 1
                            if actual.get(res, 0) == qty:
                                qty_matches += 1

                    rec = {
                        "game_id": g["game_id"],
                        "round_number": r["round_number"],
                        "agent": agent,
                        "mode": g.get("mode", ""),
                        "goal_type": g.get("goal_type", ""),
                        "condition": g.get("condition", ""),
                        "stated_resources": stated_resources,
                        "actual_resources": actual_resources,
                        "resource_overlap": resource_overlap,
                        "resource_match": resource_match,
                        "qty_match_rate": (
                            qty_matches / qty_total if qty_total > 0 else np.nan
                        ),
                        "overdrawn": r.get("overdrawn", False),
                    }
                    records.append(rec)

                    if not resource_match and resource_overlap < 0.5:
                        last_msg = agent_msgs[-1].get("message", "") if agent_msgs else ""
                        divergence_examples.append({
                            **rec,
                            "stated": all_intentions,
                            "actual": actual,
                            "last_message": last_msg[:200],
                        })

                # --- Project mention extraction ---
                total_project_mentions: dict[str, int] = {}
                msgs_with_project = 0
                for m in agent_msgs:
                    pm = extract_project_mentions(
                        m.get("message", ""), project_names=project_names,
                    )
                    if pm:
                        msgs_with_project += 1
                        for p, c in pm.items():
                            total_project_mentions[p] = total_project_mentions.get(p, 0) + c

                project_records.append({
                    "game_id": g["game_id"],
                    "round_number": r["round_number"],
                    "agent": agent,
                    "mode": g.get("mode", ""),
                    "condition": g.get("condition", ""),
                    "num_messages": len(agent_msgs),
                    "msgs_with_project_mention": msgs_with_project,
                    "project_mention_rate": (
                        msgs_with_project / len(agent_msgs) if agent_msgs else 0.0
                    ),
                    "total_project_mentions": sum(total_project_mentions.values()),
                    "projects_mentioned": set(total_project_mentions.keys()),
                    "num_distinct_projects": len(total_project_mentions),
                })

    cohere_df = pd.DataFrame(records)
    project_df = pd.DataFrame(project_records)
    return {
        "cohere_df": cohere_df,
        "project_df": project_df,
        "divergence_examples": divergence_examples[:20],
    }


def print_summary(results: dict) -> None:
    print("=" * 60)
    print("RQ6: Stated vs Actual Coherence")
    print("=" * 60)

    df = results["cohere_df"]
    if len(df) > 0:
        print(f"\nResource intention observations: {len(df)}")
        print(f"Perfect resource match: {df['resource_match'].mean():.1%}")
        print(f"Mean resource overlap (Jaccard): {df['resource_overlap'].mean():.3f}")

        qty_data = df["qty_match_rate"].dropna()
        if len(qty_data) > 0:
            print(f"Quantity match rate: {qty_data.mean():.1%} (n={len(qty_data)})")

        if "condition" in df.columns and df["condition"].nunique() > 1:
            print("\nBy condition:")
            print(
                df.groupby("condition")[["resource_overlap", "resource_match"]]
                .mean()
                .round(3)
            )
    else:
        print("\nNo resource intentions extracted.")

    # Project mention frequency
    pdf = results.get("project_df", pd.DataFrame())
    if not pdf.empty:
        print("\n" + "-" * 60)
        print("Project Mention Frequency")
        print("-" * 60)
        has_msgs = pdf[pdf["num_messages"] > 0]
        print(f"Agent-rounds with speech: {len(has_msgs)}")
        print(f"Agent-rounds mentioning projects: {(has_msgs['msgs_with_project_mention'] > 0).sum()}")
        print(f"Project mention rate (per agent-round): {has_msgs['project_mention_rate'].mean():.1%}")
        print(f"Mean project mentions per agent-round: {has_msgs['total_project_mentions'].mean():.1f}")
        print(f"Mean distinct projects mentioned: {has_msgs['num_distinct_projects'].mean():.1f}")

    if results["divergence_examples"]:
        print("\nDivergence examples (stated != actual):")
        for ex in results["divergence_examples"][:5]:
            print(f"  Game {ex['game_id'][:8]} R{ex['round_number']} [{ex['agent']}]:")
            print(f"    Stated: {ex['stated']}, Actual: {ex['actual']}")
            print(f"    Last msg: {ex['last_message'][:120]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RQ6: Stated vs Actual Intentions")
    add_common_args(parser)
    args = parser.parse_args()
    dataset = load_dataset_from_args(args)
    raw_games = [g.raw for g in dataset.games]
    results = analyze_stated_vs_actual(raw_games)
    print_summary(results)