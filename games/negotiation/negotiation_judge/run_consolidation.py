"""Phase 2: Pull all judgments from Firestore → consolidate taxonomy → CSV.

Run this after both distributed workers finish Phase 1.

Usage:
    uv run python -m judge.run_consolidation \
        --provider openrouter \
        --model z-ai/glm-4.7

    # Filter to a specific prompt version (e.g. only judgments from a particular run):
    uv run python -m judge.run_consolidation \
        --provider openrouter \
        --model z-ai/glm-4.7 \
        --prompt-version dcaa097

    # Or with a different consolidation model:
    uv run python -m judge.run_consolidation \
        --provider anthropic \
        --model claude-sonnet-4-5-20250929
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from negotiation_game.backend.defaults import REPO_ROOT, LLM_PROVIDERS
from negotiation_game.backend.agents.factory import detect_provider, get_api_key_for_provider
from negotiation_judge.schema import GameJudgment
from negotiation_judge.storage import firestore_available, list_all_judgments
from negotiation_judge.consolidate import consolidate_taxonomy, save_taxonomy, collect_patterns
from negotiation_judge.prompts import build_consolidation_prompt
from negotiation_judge.aggregate import build_judgments_csv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-20s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("judge.consolidation")


def main():
    parser = argparse.ArgumentParser(description="Phase 2: consolidate taxonomy from Firestore judgments")
    parser.add_argument("--provider", default=None, choices=list(LLM_PROVIDERS.keys()))
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--thinking-level", default=None, choices=["low", "medium", "high"],
                        help="Enable Gemini thinking mode (low/medium/high). Omit to disable.")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "judge" / "output")
    parser.add_argument("--prompt-version", default=None,
                        help="Only consolidate judgments with this prompt_version (e.g. dcaa097). "
                             "Omit to include all versions.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Pull and count judgments only, no consolidation LLM call")
    args = parser.parse_args()

    # Resolve model first, then infer provider if not given
    model = args.model or os.environ.get("JUDGE_MODEL")
    provider_name = args.provider
    if not provider_name:
        if not model:
            log.error("Specify --model or set JUDGE_MODEL so the provider can be auto-detected.")
            sys.exit(1)
        provider_name = detect_provider(model)
        if not provider_name or provider_name not in LLM_PROVIDERS:
            log.error("Could not detect provider for model '%s'. Pass --provider explicitly.", model)
            sys.exit(1)
        log.info("Auto-detected provider: %s", provider_name)

    provider = LLM_PROVIDERS[provider_name]
    api_format = provider["api_format"]
    api_base = provider["api_base"]
    model = model or provider["model"]

    if args.api_key_env:
        api_key = os.environ.get(args.api_key_env)
    else:
        api_key = get_api_key_for_provider(provider_name)
    if not api_key and not args.dry_run:
        log.error("No API key found for provider '%s'. Check your environment variables.", provider_name)
        sys.exit(1)

    if not firestore_available():
        log.error("Firestore not available. Set up GCP credentials first.")
        sys.exit(1)

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: Pull all judgments from Firestore
    log.info("Pulling all judgments from Firestore...")
    raw_docs = list_all_judgments()
    log.info("Found %d judgment documents", len(raw_docs))

    if args.prompt_version:
        before = len(raw_docs)
        raw_docs = [d for d in raw_docs if d.get("prompt_version") == args.prompt_version]
        log.info("Prompt version filter '%s': %d -> %d documents", args.prompt_version, before, len(raw_docs))
        if not raw_docs:
            log.error("No judgments found with prompt_version='%s'. Available versions: %s",
                      args.prompt_version,
                      sorted({d.get("prompt_version", "unknown") for d in list_all_judgments()}))
            sys.exit(1)

    if not raw_docs:
        log.error("No judgments found in Firestore. Run Phase 1 first.")
        sys.exit(1)

    # Parse into GameJudgment objects
    judgments: list[GameJudgment] = []
    parse_errors = 0
    for doc in raw_docs:
        try:
            # Remove Firestore metadata fields before validation
            clean = {k: v for k, v in doc.items() if k not in ("judge_model", "judged_at")}
            judgments.append(GameJudgment.model_validate(clean))
        except Exception as e:
            parse_errors += 1
            log.warning("Failed to parse judgment %s: %s", doc.get("game_id", "?"), e)

    log.info("Parsed %d judgments (%d parse errors)", len(judgments), parse_errors)

    total_rounds = sum(len(j.rounds) for j in judgments)
    total_patterns = sum(sum(len(r.patterns) for r in j.rounds) for j in judgments)
    log.info("Corpus: %d games, %d rounds, %d total pattern instances", len(judgments), total_rounds, total_patterns)

    if args.dry_run:
        # Show breakdown
        from collections import Counter
        models = Counter()
        outcomes = Counter()
        for j in judgments:
            models[f"{j.model_a} vs {j.model_b}"] += 1
            for r in j.rounds:
                outcomes[r.round_outcome.value] += 1
        print(f"\n{'='*60}")
        print("FIRESTORE JUDGMENT SUMMARY")
        print(f"{'='*60}")
        print(f"Games: {len(judgments)}")
        print(f"Rounds: {total_rounds}")
        print(f"Pattern instances: {total_patterns}")
        print(f"\nBy model pair:")
        for pair, count in models.most_common():
            print(f"  {pair}: {count}")
        print(f"\nBy outcome:")
        for outcome, count in outcomes.most_common():
            print(f"  {outcome}: {count}")
        print(f"{'='*60}")

        # Print the prompts that would be sent to the LLM for consolidation
        patterns = collect_patterns(judgments)
        messages = build_consolidation_prompt(patterns)
        print(f"\n{'='*60}")
        print(f"CONSOLIDATION PROMPT ({len(messages)} message(s))")
        print(f"{'='*60}")
        for i, msg in enumerate(messages):
            print(f"\n--- [{i}] role={msg['role']} ---")
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        print(block["text"])
            else:
                print(content)
        print(f"{'='*60}")
        return

    # Step 2: Taxonomy consolidation
    _THINKING_BUDGETS = {"low": 1024, "medium": 8192, "high": 24576}
    thinking_config = None
    if args.thinking_level:
        if api_format != "anthropic":
            log.warning(
                "--thinking-level is only supported for Anthropic models. "
                "Ignoring for api_format=%s (Gemini 2.5 Pro thinks by default).",
                api_format,
            )
        else:
            thinking_config = {"thinking_budget": _THINKING_BUDGETS[args.thinking_level]}
            log.info("Thinking mode: %s (budget=%d tokens)", args.thinking_level, thinking_config["thinking_budget"])

    log.info("Running two-step taxonomy consolidation over %d judgments...", len(judgments))
    taxonomy_path = output_dir / "taxonomy.json"
    taxonomy = consolidate_taxonomy(
        judgments=judgments,
        api_format=api_format,
        api_base=api_base,
        api_key=api_key,
        model=model,
        temperature=args.temperature,
        thinking_config=thinking_config,
        output_path=taxonomy_path,
    )
    save_taxonomy(taxonomy, taxonomy_path)
    log.info("Taxonomy saved to %s (categories + aliases)", taxonomy_path)

    # Step 3: Build CSV
    csv_path = output_dir / "judgments.csv"
    df = build_judgments_csv(judgments, csv_path, taxonomy=taxonomy)
    log.info("Wrote %s (%d rows)", csv_path, len(df))

    # Also save raw judgments locally for offline access
    raw_dir = output_dir / "raw"
    raw_dir.mkdir(exist_ok=True)
    for j in judgments:
        path = raw_dir / f"{j.game_id}.json"
        if not path.exists():
            with open(path, "w") as f:
                json.dump(j.model_dump(), f, indent=2)

    # Summary
    print(f"\n{'='*60}")
    print("CONSOLIDATION COMPLETE")
    print(f"{'='*60}")
    print(f"Games: {len(judgments)}")
    print(f"Rounds: {total_rounds}")
    print(f"Taxonomy: {len(taxonomy.negative_patterns)} failure modes + "
          f"{len(taxonomy.positive_patterns)} positive patterns")
    print(f"CSV: {csv_path} ({len(df)} rows)")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
