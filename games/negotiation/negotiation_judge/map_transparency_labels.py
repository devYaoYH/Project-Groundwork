"""Map unmatched transparency judge pattern names onto canonical taxonomy labels.

Collects all free-text pattern names from the transparency raw judge outputs that
don't match existing taxonomy aliases, then sends them in a single Gemini call to
map each onto one of the 16 canonical IDs. Saves the result as:
    judge/output/taxonomy_full_transparency.json

This file can be loaded alongside the main taxonomy.json to enrich alias matching
for transparency-condition games.

Usage:
    uv run python -m judge.map_transparency_labels [--dry-run]
"""

import argparse
import json
import logging
import os
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from negotiation_game.backend.defaults import REPO_ROOT, LLM_PROVIDERS
from negotiation_judge.aggregate import build_alias_index
from negotiation_judge.consolidate import load_taxonomy
from negotiation_analysis.data_loader import load_experiment_data
from negotiation_game.backend.agents.api import call_llm_oneshot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-20s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("judge.map_transparency_labels")

TRANSPARENCY_RUN_IDS = frozenset({
    "56abe7a8-2d59-4b7b-9d4d-80cbdd078a65",
    "ac21edea-41ae-4953-894c-0327927b0e8a",
})

RAW_DIR = REPO_ROOT / "judge" / "output" / "raw"
TAXONOMY_PATH = REPO_ROOT / "judge" / "output" / "taxonomy.json"
OUTPUT_PATH = REPO_ROOT / "judge" / "output" / "taxonomy_full_transparency.json"

MODEL = "gemini-3.1-pro-preview"
PROVIDER = "gemini"


def _normalize(s: str) -> str:
    return " ".join(s.lower().split()) if s else ""


def collect_unmatched(transp_ids: set[str], alias_index: dict) -> Counter:
    unmatched: Counter = Counter()
    for gid in transp_ids:
        path = RAW_DIR / f"{gid}.json"
        if not path.exists():
            continue
        with open(path) as f:
            d = json.load(f)
        for r in d.get("rounds", []):
            for p in r.get("patterns", []):
                name = p.get("name", "")
                if name and not alias_index.get(_normalize(name)):
                    unmatched[name] += 1
    return unmatched


def build_prompt(unmatched_names: list[str], taxonomy) -> str:
    canonical_block = "\n".join(
        f"  {p.id} | {p.label} | {'negative' if p in taxonomy.negative_patterns else 'positive'} | {p.description}"
        for p in taxonomy.negative_patterns + taxonomy.positive_patterns
    )
    names_block = "\n".join(f"  {name}" for name in sorted(unmatched_names))

    return f"""You are an expert annotator for multi-agent LLM negotiation research.

Below are 16 canonical coordination pattern labels used to classify negotiation behaviour.
Each line is: canonical_id | label | polarity | definition

CANONICAL LABELS:
{canonical_block}

Your task: map each of the following free-text pattern names (produced by a separate LLM judge)
onto exactly one canonical_id. Choose the closest semantic match. If a name genuinely does not
fit any canonical label, map it to the special value "other".

FREE-TEXT PATTERN NAMES TO MAP ({len(unmatched_names)} total, one per line):
{names_block}

Respond with ONLY a JSON object where each key is the exact free-text pattern name
and each value is the canonical_id string it maps to (one of the 16 IDs above, or "other").
No explanation, no markdown fences. Example format:
{{"some_pattern_name": "agreement_abandonment", "another_name": "other"}}"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print prompt without calling API")
    args = parser.parse_args()

    taxonomy = load_taxonomy(TAXONOMY_PATH)
    alias_index = build_alias_index(taxonomy)

    games = load_experiment_data()
    transp_ids = {g["episode_uid"] for g in games if g.get("episode_id") in TRANSPARENCY_RUN_IDS}
    log.info("Transparency environment IDs: %d", len(transp_ids))

    unmatched = collect_unmatched(transp_ids, alias_index)
    log.info("Unique unmatched names: %d  (total instances: %d)", len(unmatched), sum(unmatched.values()))

    unmatched_names = sorted(unmatched.keys())
    prompt = build_prompt(unmatched_names, taxonomy)

    if args.dry_run:
        print(prompt)
        log.info("Dry run — prompt printed, no API call made.")
        return

    provider = LLM_PROVIDERS[PROVIDER]
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise SystemExit("GOOGLE_API_KEY not set.")

    log.info("Calling %s via %s...", MODEL, PROVIDER)
    messages = [{"role": "user", "content": prompt}]
    text = call_llm_oneshot(
        api_format=provider["api_format"],
        api_base=provider["api_base"],
        api_key=api_key,
        model=MODEL,
        messages=messages,
        max_tokens=32000,
        temperature=0.0,
    )

    # Strip fences if present
    import re
    text = text.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()

    try:
        mapping: dict[str, str] = json.loads(text)
    except json.JSONDecodeError:
        from json_repair import repair_json
        mapping = json.loads(repair_json(text))

    # Validate canonical IDs
    valid_ids = {p.id for p in taxonomy.negative_patterns + taxonomy.positive_patterns} | {"other"}
    invalid = {k: v for k, v in mapping.items() if v not in valid_ids}
    if invalid:
        log.warning("Invalid canonical IDs returned for %d names: %s", len(invalid), list(invalid.items())[:5])

    # Add occurrence counts for reference
    output = {
        "description": "Alias mapping from transparency-condition free-text pattern names to canonical taxonomy IDs.",
        "source_run_ids": list(TRANSPARENCY_RUN_IDS),
        "canonical_taxonomy": str(TAXONOMY_PATH),
        "mappings": {
            name: {
                "canonical_id": mapping.get(name, "other"),
                "occurrences": unmatched[name],
            }
            for name in unmatched_names
        },
    }

    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    mapped = sum(1 for v in mapping.values() if v != "other")
    log.info("Mapped %d/%d names to canonical labels (%d -> 'other')", mapped, len(unmatched_names), len(unmatched_names) - mapped)
    log.info("Saved -> %s", OUTPUT_PATH)

    # Quick summary
    from collections import Counter
    by_canonical: Counter = Counter()
    for name in unmatched_names:
        by_canonical[mapping.get(name, "other")] += unmatched[name]
    print("\nInstance counts by canonical ID (from unmatched names):")
    for cid, cnt in by_canonical.most_common():
        print(f"  {cid:<38} {cnt:4d}")


if __name__ == "__main__":
    main()
