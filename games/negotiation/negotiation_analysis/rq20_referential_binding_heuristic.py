"""RQ20 — Referential Binding Failures: Heuristic Sampler

Distinct from RQ14 (which uses LLM classification), this script uses a
purely heuristic pipeline to surface rounds where agents verbally agreed
on a split but still submitted allocations that caused an overdraw.

Three-stage pipeline (each stage narrows the candidate set):
  1. Base Filter       — keep only overdrawn rounds
  2. Agreement Filter  — last N chat turns contain positive-agreement language
  3. Numerical Disconnect Filter — numbers mentioned in final turns differ
                                   substantially from submitted allocations

Outputs:
  - Console: ranked summary table
  - data/binding_failures_sample.md: top --top-n examples for manual review

Usage:
    uv run python -m scripts.analysis.rq20_referential_binding_heuristic
    uv run python -m scripts.analysis.rq20_referential_binding_heuristic --top-n 30 --tail-turns 3
"""

import argparse
import re
import textwrap
from pathlib import Path

import pandas as pd

from negotiation_analysis.data_loader import (
    RESOURCE_SUPPLY,
    RESOURCES,
    add_common_args,
    load_dataset_from_args,
)
from negotiation_analysis.models import NegotiationDataset, NegotiationRound

OUTPUT_DIR = Path(__file__).parent.parent.parent / "data"
DEFAULT_TOP_N = 20
DEFAULT_TAIL_TURNS = 2   # how many final agent turns to inspect

# ---------------------------------------------------------------------------
# Stage 2: Agreement keyword regex
# Matches positive commitment / acceptance pragmatics in the final turns.
# Extend this list freely — it is the main heuristic tuning knob.
# ---------------------------------------------------------------------------
AGREEMENT_PATTERN = re.compile(
    r"\b("
    r"agree[sd]?|agreed|deal|sounds good|perfect|confirm(?:ed)?|"
    r"I(?:'ll| will) take|I(?:'ll| will) go with|let(?:'s| us) go with|"
    r"that works|settled|done|great plan|yes(?:,| )|sure(?:,| )|"
    r"happy with|works for me|accept(?:ed)?|excellent|absolutely"
    r")\b",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Stage 3: Number extraction helpers
# ---------------------------------------------------------------------------
_NUMBER_RE = re.compile(r"\b(\d+)\b")


def _extract_numbers(text: str) -> list[int]:
    """Return all integers found in text."""
    return [int(m) for m in _NUMBER_RE.findall(text)]


def _mentioned_resource_numbers(turns_text: str, resources: list[str]) -> dict[str, list[int]]:
    """
    For each resource name found in the text, collect integers appearing
    within a short window (±50 chars) of that resource mention.
    """
    mentioned: dict[str, list[int]] = {r: [] for r in resources}
    for resource in resources:
        for match in re.finditer(resource, turns_text, re.IGNORECASE):
            window_start = max(0, match.start() - 50)
            window_end   = min(len(turns_text), match.end() + 50)
            window = turns_text[window_start:window_end]
            mentioned[resource].extend(_extract_numbers(window))
    return mentioned


def _numerical_disconnect_score(
    mentioned: dict[str, list[int]],
    alloc_a: dict[str, int],
    alloc_b: dict[str, int],
    supply: dict[str, int],
) -> float:
    """
    Score = mean absolute gap between the smallest number mentioned for each
    resource and the total actually submitted, normalised by supply.

    Higher score → bigger disconnect between what was discussed and submitted.
    Returns 0.0 when no numbers are mentioned (can't score).
    """
    gaps = []
    for resource in supply:
        nums = mentioned.get(resource, [])
        if not nums:
            continue
        # Most conservative interpretation: agents discussed the minimum number
        # they would each take, so smallest mention is closest to agreed split.
        min_mentioned = min(nums)
        total_submitted = alloc_a.get(resource, 0) + alloc_b.get(resource, 0)
        gap = abs(total_submitted - min_mentioned) / max(supply[resource], 1)
        gaps.append(gap)
    return sum(gaps) / len(gaps) if gaps else 0.0


# ---------------------------------------------------------------------------
# Main analysis function
# ---------------------------------------------------------------------------

def analyze_binding_failures(
    dataset: NegotiationDataset,
    tail_turns: int = DEFAULT_TAIL_TURNS,
) -> pd.DataFrame:
    """
    Run the three-stage heuristic pipeline across all overdrawn rounds.

    Returns a DataFrame with one row per candidate round, sorted by
    descending egregious_score (agreement_strength × numerical_disconnect).
    """
    records = []

    for g in dataset.games:
        resource_types = g.config.get("resource_types", RESOURCES)
        supply = g.config.get("resource_supply", RESOURCE_SUPPLY)

        for r in g.rounds:

            # ── Stage 1: Base filter ────────────────────────────────────────
            if not r.overdrawn:
                continue

            # Collect agent turns that have a public speech event
            speech_turns = [
                t for t in r.turns
                if t.speaker in ("agent_a", "agent_b") and t.speech is not None
            ]
            if not speech_turns:
                continue

            tail = speech_turns[-tail_turns:]
            tail_text = " ".join(t.speech.content for t in tail)

            # ── Stage 2: Agreement filter ───────────────────────────────────
            agreement_hits = AGREEMENT_PATTERN.findall(tail_text)
            if not agreement_hits:
                continue
            agreement_strength = len(agreement_hits)  # more hits = stronger signal

            # ── Stage 3: Numerical disconnect ───────────────────────────────
            mentioned = _mentioned_resource_numbers(tail_text, resource_types)
            alloc_a   = r.agent_a_resources
            alloc_b   = r.agent_b_resources
            disconnect = _numerical_disconnect_score(mentioned, alloc_a, alloc_b, supply)

            # Composite egregious score: both signals must be present to rank high
            egregious_score = agreement_strength * (1.0 + disconnect)

            # Overdraw magnitude: total units submitted above supply across all resources
            overdraw_units = sum(
                max(0, alloc_a.get(r_name, 0) + alloc_b.get(r_name, 0) - supply.get(r_name, 0))
                for r_name in resource_types
            )

            records.append({
                "episode_uid":            g.episode_uid,
                "round_number":       r.round_number,
                "mode":               g.mode,
                "mc_bucket":          g.metadata.get("mc_bucket"),
                "model_a":            g.model_a,
                "model_b":            g.model_b,
                "agreement_strength": agreement_strength,
                "agreement_hits":     agreement_hits[:5],   # first 5 matched phrases
                "disconnect_score":   round(disconnect, 3),
                "egregious_score":    round(egregious_score, 3),
                "overdraw_units":     overdraw_units,
                "alloc_a":            alloc_a,
                "alloc_b":            alloc_b,
                "supply":             supply,
                "tail_text":          tail_text,
                # Keep full turn list for Markdown rendering
                "_speech_turns":      speech_turns,
                "_round":             r,
            })

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records).sort_values("egregious_score", ascending=False)
    return df.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------

def _render_allocation_table(
    alloc_a: dict[str, int],
    alloc_b: dict[str, int],
    supply: dict[str, int],
) -> str:
    resources = sorted(set(alloc_a) | set(alloc_b) | set(supply))
    header = "| Resource | Supply | Agent A | Agent B | Joint | Overdraw |\n"
    sep    = "|----------|--------|---------|---------|-------|----------|\n"
    rows   = []
    for res in resources:
        s  = supply.get(res, "?")
        a  = alloc_a.get(res, 0)
        b  = alloc_b.get(res, 0)
        j  = a + b
        od = "⚠️ YES" if isinstance(s, int) and j > s else "—"
        rows.append(f"| {res:<8} | {s:<6} | {a:<7} | {b:<7} | {j:<5} | {od} |")
    return header + sep + "\n".join(rows)


def _render_transcript(speech_turns: list, tail_n: int) -> str:
    lines = []
    tail_start = max(0, len(speech_turns) - tail_n)
    for i, turn in enumerate(speech_turns):
        agent_label = "**Agent A**" if turn.speaker == "agent_a" else "**Agent B**"
        marker = " ◀ *agreement window*" if i >= tail_start else ""
        wrapped = textwrap.fill(turn.speech.content if turn.speech else "", width=100, subsequent_indent="  ")
        lines.append(f"{agent_label} (turn {turn.turn_number}){marker}\n\n  {wrapped}\n")
    return "\n".join(lines)


def write_markdown(df: pd.DataFrame, top_n: int, tail_turns: int, output_path: Path) -> None:
    candidates = df.head(top_n)

    md_lines = [
        "# Referential Binding Failures — Heuristic Sample\n",
        f"Generated by `rq20_referential_binding_heuristic.py`  \n",
        f"Pipeline: overdrawn → agreement keywords in last {tail_turns} turns "
        f"→ numerical disconnect score  \n",
        f"Showing top {len(candidates)} examples ranked by egregious score "
        f"(agreement strength × numerical disconnect).\n",
        "---\n",
    ]

    for rank, (_, row) in enumerate(candidates.iterrows(), start=1):
        md_lines += [
            f"## Example {rank} — Environment `{row['episode_uid'][:12]}` · Round {row['round_number']}\n",
            f"**Mode:** {row['mode']}  |  **M/C bucket:** {row['mc_bucket']}  "
            f"|  **Models:** `{row['model_a']}` vs `{row['model_b']}`  \n",
            f"**Egregious score:** {row['egregious_score']:.2f}  "
            f"|  **Agreement hits:** {', '.join(f'`{h}`' for h in row['agreement_hits'])}  "
            f"|  **Numerical disconnect:** {row['disconnect_score']:.3f}  "
            f"|  **Overdraw units:** {row['overdraw_units']}\n",
            "\n### Chat Transcript\n",
            _render_transcript(row["_speech_turns"], tail_turns),
            "\n### Submitted Allocations vs Supply\n",
            _render_allocation_table(row["alloc_a"], row["alloc_b"], row["supply"]),
            "\n---\n",
        ]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"\nWrote {len(candidates)} examples → {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="RQ20: Heuristic referential binding failure sampler"
    )
    add_common_args(parser)
    parser.add_argument(
        "--top-n", type=int, default=DEFAULT_TOP_N,
        help=f"Number of top examples to write to Markdown (default: {DEFAULT_TOP_N})",
    )
    parser.add_argument(
        "--tail-turns", type=int, default=DEFAULT_TAIL_TURNS,
        help=f"Number of final agent turns to inspect for agreement language (default: {DEFAULT_TAIL_TURNS})",
    )
    parser.add_argument(
        "--output", type=Path, default=OUTPUT_DIR / "binding_failures_sample.md",
        help="Output Markdown path",
    )
    args = parser.parse_args()

    dataset = load_dataset_from_args(args)

    print(f"\nRunning heuristic pipeline on {len(dataset.games)} games...")
    df = analyze_binding_failures(dataset, tail_turns=args.tail_turns)

    if df.empty:
        print("No candidates found. Try relaxing the filters.")
    else:
        print(f"\nStage results:")
        total_rounds   = sum(len(g.rounds) for g in dataset.games)
        overdrawn      = sum(1 for g in dataset.games for r in g.rounds if r.overdrawn)
        print(f"  Total rounds       : {total_rounds}")
        print(f"  Overdrawn (stage 1): {overdrawn}  ({overdrawn/max(total_rounds,1):.1%})")
        print(f"  + Agreement (stage 2+3): {len(df)}  ({len(df)/max(overdrawn,1):.1%} of overdrawn)")

        display_cols = [
            "episode_uid", "round_number", "mode", "mc_bucket",
            "model_a", "agreement_strength", "disconnect_score", "egregious_score", "overdraw_units",
        ]
        print("\nTop candidates (truncated episode_uid):")
        top = df[display_cols].head(args.top_n).copy()
        top["episode_uid"] = top["episode_uid"].str[:10]
        print(top.to_string(index=False))

        write_markdown(df, top_n=args.top_n, tail_turns=args.tail_turns, output_path=args.output)
