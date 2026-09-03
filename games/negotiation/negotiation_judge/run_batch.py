"""Cell pipeline entrypoint for the LLM judge.

Two phases per run (reclassification is dropped for now):
  Phase 0: Extraction — write JudgeGameContext JSON to output/extracted/<episode_uid>.json
  Phase 1: Discovery  — one LLM call per environment, save raw judgment
  Phase 2: Consolidation — one LLM call over all raw judgments, save taxonomy
  CSV:     Flatten raw environment judgments into judgments.csv

Usage:
    uv run python -m judge.run_cell \
        --provider anthropic \
        --model claude-sonnet-4-5-20250929 \
        --output-dir judge/output
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
from negotiation_analysis.data_loader import load_experiment_data
from negotiation_analysis.models import NegotiationDataset
from negotiation_judge.extractor import extract_all_game_contexts
from negotiation_judge.judge import judge_game
from negotiation_judge.consolidate import consolidate_taxonomy, save_taxonomy
from negotiation_judge.schema import GameJudgment, JudgeGameContext
from negotiation_judge.aggregate import build_judgments_csv

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-20s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("judge.cell")


def _extracted_path(output_dir: Path, episode_uid: str) -> Path:
    return output_dir / "extracted" / f"{episode_uid}.json"


INTERVENTION_KEYWORDS = {"share", "tom", "named", "transp", "transparency", "joint", "notalk"}


def _has_intervention(ctx, dataset: NegotiationDataset) -> bool:
    """Check if a environment has any intervention flags set in its config or label."""
    environment = next((g for g in dataset.games if g.episode_uid == ctx.episode_uid), None)
    if environment is None:
        return False
    label = environment.label.lower()
    cfg = environment.config
    if cfg.get("share_projects", False) or cfg.get("think_about_opponent", False) or cfg.get("named_projects", False):
        return True
    return any(kw in label for kw in INTERVENTION_KEYWORDS)


def _raw_path(output_dir: Path, episode_uid: str) -> Path:
    return output_dir / "raw" / f"{episode_uid}.json"


def _save_extracted_context(ctx: JudgeGameContext, output_dir: Path) -> None:
    """Persist a JudgeGameContext to disk for inspection and reproducibility."""
    path = _extracted_path(output_dir, ctx.episode_uid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(ctx.model_dump(), f, indent=2)


def _load_existing_games(output_dir: Path) -> dict[str, GameJudgment]:
    """Load already-completed environment judgments for resume support."""
    raw_dir = output_dir / "raw"
    existing = {}
    if not raw_dir.exists():
        return existing
    for f in raw_dir.glob("*.json"):
        try:
            with open(f) as fh:
                data = json.load(fh)
            # Skip legacy per-round files (they lack top-level "rounds" list)
            if "rounds" not in data or not isinstance(data["rounds"], list):
                continue
            j = GameJudgment.model_validate(data)
            existing[j.episode_uid] = j
        except Exception as e:
            log.warning("Skipping corrupt judgment file %s: %s", f, e)
    return existing


def main():
    parser = argparse.ArgumentParser(description="Run LLM judge cell pipeline (per-environment)")
    parser.add_argument("--provider", default="anthropic", choices=list(LLM_PROVIDERS.keys()),
                        help="LLM provider (default: anthropic)")
    parser.add_argument("--model", default=None,
                        help="Judge model (default: provider default)")
    parser.add_argument("--api-key-env", default=None,
                        help="Env var name for API key (auto-detected from provider)")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "judge" / "output",
                        help="Output directory for judgments")
    parser.add_argument("--temperature", type=float, default=0.0,
                        help="Judge temperature (default: 0.0)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Limit number of games to judge (for testing)")
    parser.add_argument("--min-schema-version", type=int, default=5,
                        help="Minimum schema version to include (default: 5)")
    parser.add_argument("--models", nargs="+", default=None,
                        help="Only include games where BOTH agents are in this list (e.g., --models gpt-5-mini claude-sonnet-4-5)")
    parser.add_argument("--exclude-interventions", action="store_true",
                        help="Exclude games with intervention flags (share, tom, named, transparency, joint, notalk)")
    parser.add_argument("--consolidate", action="store_true", default=True,
                        help="Run taxonomy consolidation after judging (default: True)")
    parser.add_argument("--no-consolidate", action="store_true",
                        help="Skip taxonomy consolidation")
    parser.add_argument("--resume", action="store_true", default=True,
                        help="Skip games with existing judgments (default: True)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Extract contexts only, no API calls")
    args = parser.parse_args()

    if args.no_consolidate:
        args.consolidate = False

    # Resolve provider config
    provider = LLM_PROVIDERS[args.provider]
    api_format = provider["api_format"]
    api_base = provider["api_base"]
    model = args.model or os.environ.get("JUDGE_MODEL") or provider["model"]

    # Resolve API key
    key_env_map = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }
    key_env = args.api_key_env or key_env_map.get(args.provider, "API_KEY")
    api_key = os.environ.get(key_env)
    if not api_key and not args.dry_run:
        log.error("API key not found. Set %s release variable.", key_env)
        sys.exit(1)

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "extracted").mkdir(exist_ok=True)
    (output_dir / "raw").mkdir(exist_ok=True)

    # Step 0: Load data and extract GAME contexts
    log.info("Loading experiment data...")
    raw_traces = load_experiment_data()
    dataset = NegotiationDataset.from_traces(raw_traces)
    contexts = extract_all_game_contexts(dataset, min_schema_version=args.min_schema_version)

    # Filter by model if specified
    if args.models:
        model_set = set(args.models)
        before = len(contexts)
        contexts = [c for c in contexts if c.model_a in model_set and c.model_b in model_set]
        log.info("Model filter %s: %d -> %d games", model_set, before, len(contexts))

    # Filter out intervention experiments
    if args.exclude_interventions:
        before = len(contexts)
        contexts = [c for c in contexts if not _has_intervention(c, dataset)]
        log.info("Intervention filter: %d -> %d games", before, len(contexts))

    total_rounds = sum(len(c.rounds) for c in contexts)
    log.info("Extracted %d games (%d total rounds)", len(contexts), total_rounds)

    if args.limit:
        contexts = contexts[:args.limit]
        log.info("Limited to %d games", len(contexts))

    # Persist extracted contexts to disk (always, for inspection and reproducibility)
    log.info("Writing extracted contexts to %s/extracted/", output_dir)
    for ctx in contexts:
        _save_extracted_context(ctx, output_dir)

    if args.dry_run:
        for ctx in contexts:
            log.info("  %s: %d rounds | %s vs %s | %s mc=%s",
                     ctx.episode_uid[:12], len(ctx.rounds), ctx.model_a, ctx.model_b, ctx.mode, ctx.mc_ratio)
        log.info("Dry run complete. %d games would be judged.", len(contexts))
        return

    # Step 1: Judge each environment (one LLM call per environment)
    existing = _load_existing_games(output_dir) if args.resume else {}
    log.info("Found %d existing environment judgments (resume=%s)", len(existing), args.resume)

    judgments: list[GameJudgment] = list(existing.values())
    new_count = 0

    for i, ctx in enumerate(contexts):
        if ctx.episode_uid in existing:
            log.info("[%d/%d] SKIP %s (cached, %d rounds)", i + 1, len(contexts), ctx.episode_uid[:12], len(ctx.rounds))
            continue

        log.info("[%d/%d] Judging %s (%d rounds, %s vs %s, %s mc=%s)...",
                 i + 1, len(contexts), ctx.episode_uid[:12], len(ctx.rounds),
                 ctx.model_a, ctx.model_b, ctx.mode, ctx.mc_ratio)

        try:
            judgment = judge_game(
                ctx=ctx,
                api_format=api_format,
                api_base=api_base,
                api_key=api_key,
                model=model,
                temperature=args.temperature,
            )
            # Save immediately (crash-safe)
            path = _raw_path(output_dir, ctx.episode_uid)
            with open(path, "w") as f:
                json.dump(judgment.model_dump(), f, indent=2)

            judgments.append(judgment)
            new_count += 1
            total_patterns = sum(len(r.patterns) for r in judgment.rounds)
            log.info("  -> %d rounds, %d total patterns", len(judgment.rounds), total_patterns)

        except Exception as e:
            log.error("FAILED %s: %s", ctx.episode_uid, e)

    log.info("Phase 1 complete: %d new + %d cached = %d total environment judgments", new_count, len(existing), len(judgments))

    if not judgments:
        log.warning("No judgments to aggregate; skipping CSV.")
        return

    taxonomy = None
    if args.consolidate:
        # Step 2: Taxonomy consolidation
        log.info("Running taxonomy consolidation...")
        taxonomy = consolidate_taxonomy(
            judgments=judgments,
            api_format=api_format,
            api_base=api_base,
            api_key=api_key,
            model=model,
            temperature=args.temperature,
        )
        taxonomy_path = output_dir / "taxonomy.json"
        save_taxonomy(taxonomy, taxonomy_path)
    else:
        log.info("Skipping taxonomy consolidation.")

    # Step 3: Emit flat CSV from raw judgments (no canonical_id — reclassification
    # is intentionally skipped for now; pattern_name is the free-text label).
    csv_path = output_dir / "judgments.csv"
    df = build_judgments_csv(judgments, csv_path, taxonomy=taxonomy, contexts=contexts)
    log.info("Wrote %s (%d rows)", csv_path, len(df))

    # Summary
    total_rounds_judged = sum(len(j.rounds) for j in judgments)
    print("\n" + "=" * 60)
    print("JUDGE BATCH COMPLETE")
    print("=" * 60)
    print(f"Games judged: {len(judgments)} ({new_count} new)")
    print(f"Total rounds: {total_rounds_judged}")
    if taxonomy is not None:
        print(f"Canonical patterns: {len(taxonomy.negative_patterns)} failure modes + "
              f"{len(taxonomy.positive_patterns)} positive patterns")
    print(f"Output: {output_dir}")
    print(f"CSV: {csv_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
