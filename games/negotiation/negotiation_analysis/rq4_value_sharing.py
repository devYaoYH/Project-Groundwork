"""RQ4: When Do Agents Share Private Information (Value Sharing Rate)?

Detect when agents reveal private valuations or project information in cheap talk.

V1-V4: Detects value-function disclosures (e.g. "wood is worth 3 points to me").
V5+: Detects project info disclosures (requirements, rewards, project names).

Usage:
    uv run python -m scripts.analysis.rq4_value_sharing
"""

import re
import argparse

import pandas as pd

from negotiation_analysis.data_loader import add_common_args, load_dataset_from_args, RESOURCES
from negotiation_analysis.models import NegotiationDataset

_RES_PATTERN = "|".join(RESOURCES)

# V1-V4: Value-function disclosure patterns
VALUE_SHARE_PATTERNS = [
    # Direct value statements
    rf"(?:i|my)\s+(?:value|prioriti[sz]e|prefer|want|need|care about|focus on|am going for)\s+(?:the\s+)?(?:{_RES_PATTERN})",
    rf"(?:{_RES_PATTERN})\s+(?:is|are)\s+(?:my|the)\s+(?:priority|focus|top|best|highest|most valuable|most important)",
    rf"(?:{_RES_PATTERN})\s+(?:gives?|provides?|worth|earns?)\s+(?:me\s+)?(?:the\s+)?(?:most|highest|best|a lot|\d+)",
    # Explicit numerical values
    rf"(?:{_RES_PATTERN})\s+(?:is\s+)?(?:worth\s+)?\d+(?:\.\d+)?\s+(?:points?|reward|to me|for me|each)",
    rf"(?:my|i)\s+(?:values?|valuation|reward)\s+(?:is|are|for).*?(?:{_RES_PATTERN})",
    # Priority rankings
    rf"my\s+(?:top|highest|best|main|primary)\s+(?:priority|resource|pick|choice)\s+is\s+(?:{_RES_PATTERN})",
]

# V5+: Project info disclosure patterns
PROJECT_SHARE_PATTERNS = [
    # Project requirements disclosure
    rf"(?:project|plan)\s*\w*\s*(?:requires?|needs?|costs?|uses?)\s+.*?(?:{_RES_PATTERN})",
    rf"(?:my|i)\s+(?:have|need|want)\s+.*?(?:project|plan)",
    rf"(?:{_RES_PATTERN}).*?(?:for|to run|to complete)\s+(?:my\s+)?(?:project|plan)",
    # Project reward disclosure
    rf"(?:project|plan)\s*\w*\s*(?:gives?|pays?|rewards?|earns?|worth|yields?)\s+\d+",
    rf"\d+\s+(?:points?|reward)\s+(?:per|each|for)\s+(?:run|completion|project|plan)",
    # Project structure disclosure
    rf"(?:my|i)\s+(?:am|'m)\s+(?:working on|doing|running|building|completing)\s+(?:project|plan)",
    rf"(?:my|i)\s+(?:have|got)\s+(?:a\s+)?(?:project|plan)\s+(?:that|which)",
    # Specific resource-for-project statements
    rf"(?:i|my)\s+(?:need|require|use)\s+\d+\s+(?:{_RES_PATTERN})\s+(?:per|each|for)",
    rf"(?:each|per|one)\s+(?:run|copy|unit)\s+(?:of\s+)?(?:my\s+)?(?:project|plan)\s+(?:needs?|requires?|costs?|uses?)",
]

VALUE_SHARE_RE = re.compile("|".join(VALUE_SHARE_PATTERNS), re.IGNORECASE)
PROJECT_SHARE_RE = re.compile("|".join(PROJECT_SHARE_PATTERNS), re.IGNORECASE)


def _shares_info(msg: str, schema_version: int = 1) -> bool:
    """Check if a message discloses private information."""
    m = str(msg)
    if schema_version >= 5:
        return bool(PROJECT_SHARE_RE.search(m))
    return bool(VALUE_SHARE_RE.search(m))


def analyze_value_sharing(dataset: NegotiationDataset) -> dict:
    """Detect and aggregate value-sharing behaviour.

    For V5+ games, detects project info disclosure instead of value-function disclosure.
    """
    round_df = dataset.to_round_df()
    turn_df = dataset.to_turn_df()

    # Build game_id -> schema_version lookup from round_df
    sv_map = round_df.set_index("game_id")["schema_version"].to_dict()
    turn_df = turn_df.copy()
    turn_df["schema_version"] = turn_df["game_id"].map(sv_map).fillna(1).astype(int)
    turn_df["shares_values"] = turn_df.apply(
        lambda row: _shares_info(row["message"], row["schema_version"]), axis=1
    )

    # By round
    sharing_by_round = (
        turn_df.groupby(["game_id", "round_number", "mode"])
        .agg(
            any_sharing=pd.NamedAgg(column="shares_values", aggfunc="any"),
            n_sharing_msgs=pd.NamedAgg(column="shares_values", aggfunc="sum"),
            n_msgs=pd.NamedAgg(column="message", aggfunc="count"),
        )
        .reset_index()
    )
    sharing_by_round["sharing_rate"] = (
        sharing_by_round["n_sharing_msgs"] / sharing_by_round["n_msgs"]
    )

    # By agent
    agent_sharing = (
        turn_df.groupby(["speaker", "round_number"])
        .agg(sharing_rate=pd.NamedAgg(column="shares_values", aggfunc="mean"))
        .reset_index()
    )

    # By turn number
    turn_sharing = (
        turn_df.groupby("turn_number")["shares_values"].mean().reset_index()
    )

    # Example messages
    shared_msgs = turn_df[turn_df["shares_values"]]
    examples = shared_msgs.sample(
        min(10, len(shared_msgs)), random_state=42
    ) if len(shared_msgs) > 0 else pd.DataFrame()

    return {
        "turn_df": turn_df,
        "sharing_by_round": sharing_by_round,
        "agent_sharing": agent_sharing,
        "turn_sharing": turn_sharing,
        "examples": examples,
        "overall_rate": turn_df["shares_values"].mean(),
    }


def print_summary(results: dict) -> None:
    print("=" * 60)
    print("RQ4: Value Sharing Rate (Private Info Disclosure)")
    print("=" * 60)

    print(f"\nOverall value-sharing rate: {results['overall_rate']:.1%}")

    print("\nSharing rate by round × mode:")
    sr = results["sharing_by_round"]
    if "mode" in sr.columns:
        by_mode = sr.groupby(["mode", "round_number"])["sharing_rate"].mean()
        print(by_mode.round(3).to_string())

    print("\nSharing rate by turn number:")
    print(results["turn_sharing"].to_string(index=False))

    if len(results["examples"]) > 0:
        print("\nExample value-sharing messages:")
        for _, row in results["examples"].iterrows():
            print(
                f"  R{row['round_number']}T{row['turn_number']} "
                f"[{row['speaker']}]: {row['message'][:150]}"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RQ4: Value Sharing Over Time")
    add_common_args(parser)
    args = parser.parse_args()
    dataset = load_dataset_from_args(args)
    results = analyze_value_sharing(dataset)
    print_summary(results)