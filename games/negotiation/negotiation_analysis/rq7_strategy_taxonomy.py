"""RQ7: Strategy Taxonomy.

Classify negotiation strategies used across collaborative vs competitive conditions.
Per-agent breakdown for shifting mode (agent_a=full context, agent_b=reset context).

Usage:
    uv run python -m scripts.analysis.rq7_strategy_taxonomy
"""

import re
import argparse

import pandas as pd

from negotiation_analysis.data_loader import add_common_args, load_games_from_args, load_experiment_data


STRATEGY_PATTERNS: dict[str, re.Pattern] = {
    "propose_split": re.compile(
        r"(?:split|divide|share|you take.*i(?:'ll| will) take|how about|"
        r"let's split|partition)",
        re.IGNORECASE,
    ),
    "request_info": re.compile(
        r"(?:what(?:'s| is| are)\s+your|do you\s+(?:want|prefer|value|need|care)|"
        r"which\s+resource|what\s+resource|tell me)",
        re.IGNORECASE,
    ),
    "deception": re.compile(
        r"(?:trick|mislead|bluff|pretend|actually i|lied|deceiv|not really|"
        r"don't actually)",
        re.IGNORECASE,
    ),
    "cooperation": re.compile(
        r"(?:cooperat|collaborat|work together|mutual|both benefit|win-win|"
        r"help each other|maximize together|joint)",
        re.IGNORECASE,
    ),
    "competitive_threat": re.compile(
        r"(?:compet|beat|outperform|my advantage|i need to win|maximize my|"
        r"better than you|i'll take more)",
        re.IGNORECASE,
    ),
    "tit_for_tat": re.compile(
        r"(?:last round you (?:did|took|bought|chose)|previously you|"
        r"same as (?:last|before)|reciprocat|"
        r"since you (?:took|chose|picked|bought|did that)|"
        r"you did (?:that|the same) last)",
        re.IGNORECASE,
    ),
    "fairness_appeal": re.compile(
        r"(?:fair(?:ly|ness)?|equal(?:ly)?|equitab|even split|50.50|half and half)",
        re.IGNORECASE,
    ),
    "specialization": re.compile(
        r"(?:speciali[sz]|focus on different|complementary|"
        r"you take \w+ (?:and|,) i(?:'ll| will) take|"
        r"each (?:of us|take different))",
        re.IGNORECASE,
    ),
    "concession": re.compile(
        r"(?:i(?:'ll| will)\s+(?:back off|reduce|lower|step back|give up|"
        r"leave|let you have|concede|yield|defer)|you can have|take less)",
        re.IGNORECASE,
    ),
    "anchoring": re.compile(
        r"(?:i(?:'m| am)\s+(?:definitely|certainly|committed to|set on|firm)|"
        r"my final|i won't change|this is what i)",
        re.IGNORECASE,
    ),
}

STRAT_COLS = list(STRATEGY_PATTERNS.keys())


def classify_strategies(messages: list[str]) -> dict[str, int]:
    """Count strategy types used across a list of messages."""
    counts = {s: 0 for s in STRATEGY_PATTERNS}
    for msg in messages:
        for strategy, pattern in STRATEGY_PATTERNS.items():
            if pattern.search(str(msg)):
                counts[strategy] += 1
    return counts


def classify_strategies_by_agent(
    transcript: list[dict],
) -> dict[str, dict[str, int]]:
    """Classify strategies separately for each agent in a transcript.

    Returns {"agent_a": {strategy: count}, "agent_b": {strategy: count}}.
    """
    by_agent: dict[str, list[str]] = {"agent_a": [], "agent_b": []}
    for entry in transcript:
        if entry.get("type") == "thinking":
            continue
        speaker = entry.get("speaker", "")
        if speaker in by_agent:
            by_agent[speaker].append(entry.get("message", ""))

    return {
        speaker: classify_strategies(msgs)
        for speaker, msgs in by_agent.items()
    }


def extract_strategy_examples(
    raw_games: list[dict], max_per_strategy: int = 3,
) -> dict[str, list[dict]]:
    """Extract real transcript excerpts for each strategy pattern."""
    examples: dict[str, list[dict]] = {s: [] for s in STRATEGY_PATTERNS}
    for g in raw_games:
        for r in g["rounds"]:
            for entry in r.get("cheap_talk_transcript", []):
                if entry.get("type") == "thinking":
                    continue
                msg = entry.get("message", "")
                speaker = entry.get("speaker", "")
                if speaker not in ("agent_a", "agent_b"):
                    continue
                for strat, pattern in STRATEGY_PATTERNS.items():
                    if len(examples[strat]) >= max_per_strategy:
                        continue
                    if pattern.search(msg):
                        examples[strat].append({
                            "game_id": g["game_id"],
                            "condition": g["condition"],
                            "round": r["round_number"],
                            "speaker": speaker,
                            "message": msg[:300],
                        })
    return examples


def analyze_strategies(raw_games: list[dict]) -> dict:
    """Classify and aggregate strategy use per round and per agent."""
    round_records = []
    agent_records = []

    for g in raw_games:
        for r in g["rounds"]:
            ct = r.get("cheap_talk_transcript", [])
            public_msgs = [
                e.get("message", "")
                for e in ct
                if e.get("type") != "thinking"
            ]
            meta = {
                "game_id": g["game_id"],
                "round_number": r["round_number"],
                "condition": g["condition"],
                "mode": g["mode"],
                "goal_type": g["goal_type"],
                "overdrawn": r.get("overdrawn", False),
            }

            # Per-round (all agents combined)
            strat_counts = classify_strategies(public_msgs)
            round_records.append({**meta, **strat_counts})

            # Per-agent
            agent_strats = classify_strategies_by_agent(ct)
            for speaker, counts in agent_strats.items():
                agent_records.append({**meta, "speaker": speaker, **counts})

    strat_df = pd.DataFrame(round_records)
    strat_agent_df = pd.DataFrame(agent_records)

    # Round-level summaries
    by_condition = strat_df.groupby("condition")[STRAT_COLS].mean()
    by_condition_props = by_condition.div(by_condition.sum(axis=1), axis=0)
    total_counts = strat_df[STRAT_COLS].sum().sort_values(ascending=False)

    # Agent-level summaries
    by_condition_agent = (
        strat_agent_df.groupby(["condition", "speaker"])[STRAT_COLS].mean()
    )

    # Shifting mode: agent_a (full context) vs agent_b (reset context)
    shifting_agents = strat_agent_df[strat_agent_df["mode"] == "shifting"]
    shifting_agent_comparison = (
        shifting_agents.groupby("speaker")[STRAT_COLS].mean()
        if len(shifting_agents) > 0
        else pd.DataFrame()
    )

    # Strategy examples
    strategy_examples = extract_strategy_examples(raw_games)

    return {
        "strat_df": strat_df,
        "strat_agent_df": strat_agent_df,
        "by_condition": by_condition,
        "by_condition_props": by_condition_props,
        "total_counts": total_counts,
        "by_condition_agent": by_condition_agent,
        "shifting_agent_comparison": shifting_agent_comparison,
        "strategy_examples": strategy_examples,
    }


def print_summary(results: dict) -> None:
    print("=" * 60)
    print("RQ7: Strategy Taxonomy")
    print("=" * 60)

    print("\nTotal strategy instances:")
    for s, c in results["total_counts"].items():
        print(f"  {s}: {c:.0f}")

    print("\nMean strategy counts per round by condition:")
    print(results["by_condition"].round(3).to_string())

    print("\nStrategy proportions by condition:")
    print(results["by_condition_props"].round(3).to_string())

    print("\n--- Per-Agent Breakdown ---")
    print("\nMean strategy counts by condition × agent:")
    print(results["by_condition_agent"].round(3).to_string())

    if not results["shifting_agent_comparison"].empty:
        print("\nShifting mode: agent_a (full context) vs agent_b (reset):")
        print(results["shifting_agent_comparison"].round(3).to_string())

    print("\n--- Strategy Examples ---")
    for strat, examples in results["strategy_examples"].items():
        if not examples:
            continue
        print(f"\n  {strat}:")
        for ex in examples:
            print(f"    [{ex['condition']}] [{ex['speaker']}] R{ex['round']}:")
            print(f"      {ex['message'][:150]}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RQ7: Strategy Taxonomy")
    add_common_args(parser)
    args = parser.parse_args()
    raw_games = load_games_from_args(args)
    results = analyze_strategies(raw_games)
    print_summary(results)