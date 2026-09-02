"""Distributed Phase 1: judge games and write results to Firestore.

Two people can run this concurrently with different shards and API keys.
Resume is automatic — checks Firestore for already-judged game_ids.

Usage:
    # Person 1 (first half)
    uv run python -m judge.run_distributed \
        --shard 0/2 \
        --provider openrouter \
        --model z-ai/glm-4.7 \
        --min-schema-version 7 \
        --models qwen/qwen3.5-flash-02-23 gpt-5-mini claude-sonnet-4-5 \
        --exclude-interventions

    # Person 2 (second half)
    uv run python -m judge.run_distributed \
        --shard 1/2 \
        --provider openrouter \
        --model z-ai/glm-4.7 \
        --min-schema-version 7 \
        --models qwen/qwen3.5-flash-02-23 gpt-5-mini claude-sonnet-4-5 \
        --exclude-interventions

    # After both finish, one person runs:
    uv run python -m judge.run_consolidation
"""

import argparse
import json
import logging
import os
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from negotiation_game.backend.defaults import REPO_ROOT, LLM_PROVIDERS
from negotiation_game.backend.agents.factory import detect_provider, get_api_key_for_provider
from negotiation_analysis.data_loader import load_experiment_data, load_from_cache, CACHE_PATH
from negotiation_analysis.models import NegotiationDataset
from negotiation_game.utils.parallel import run_with_parallelism
from negotiation_judge.extractor import extract_all_game_contexts
from negotiation_judge.judge import judge_game, TokenBudgetExceeded
from negotiation_judge.prompts import get_prompt_version, build_judge_user_prompt
from negotiation_judge.schema import JudgeGameContext
from negotiation_judge.storage import firestore_available, save_judgment, list_completed_game_ids

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-20s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("judge.distributed")

INTERVENTION_KEYWORDS = {"share", "tom", "named", "transp", "transparency", "joint", "notalk"}


def _has_intervention(ctx: JudgeGameContext, dataset: NegotiationDataset) -> bool:
    game = next((g for g in dataset.games if g.game_id == ctx.game_id), None)
    if game is None:
        return False
    label = game.label.lower()
    cfg = game.config
    if cfg.get("share_projects", False) or cfg.get("think_about_opponent", False) or cfg.get("named_projects", False):
        return True
    return any(kw in label for kw in INTERVENTION_KEYWORDS)


def main():
    parser = argparse.ArgumentParser(description="Distributed Phase 1: judge games → Firestore")
    parser.add_argument("--shard", default=None,
                        help="Shard spec: INDEX/TOTAL (e.g., 0/2 for first half, 1/2 for second half)")
    parser.add_argument("--provider", default=None, choices=list(LLM_PROVIDERS.keys()),
                        help="LLM provider (auto-detected from --model if omitted)")
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--thinking-level", default=None, choices=["low", "medium", "high"],
                        help="Enable Gemini thinking mode (low/medium/high). Omit to disable.")
    parser.add_argument("--min-schema-version", type=int, default=7)
    parser.add_argument("--models", nargs="+", default=None,
                        help="Only games where BOTH agents are in this list")
    parser.add_argument("--run-ids", nargs="+", default=None,
                        help="Only include games whose experiment_run_id starts with one of these prefixes")
    parser.add_argument("--exclude-interventions", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--max-parallelism", type=int, default=1,
                        help="Number of concurrent judge calls (default: 1 = sequential)")
    parser.add_argument("--output-dir", type=Path, default=REPO_ROOT / "judge" / "output",
                        help="Local output directory (writes raw/<game_id>.json alongside Firestore)")
    parser.add_argument("--cache", action="store_true", default=False,
                        help="Load game traces from local cache instead of Firestore (faster startup)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Resolve model first, then infer provider if not explicitly given
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

    # Verify prompts.py is clean and capture the git hash for provenance
    prompt_version = get_prompt_version()
    log.info("Prompt version (git hash): %s", prompt_version)

    # Check Firestore
    if not firestore_available() and not args.dry_run:
        log.error("Firestore not available. Set up GCP credentials first.")
        sys.exit(1)

    # Load and filter
    if args.cache:
        log.info("Loading experiment data from local cache: %s", CACHE_PATH)
        raw_traces = load_from_cache(CACHE_PATH)
    else:
        log.info("Loading experiment data from Firestore...")
        raw_traces = load_experiment_data()
    dataset = NegotiationDataset.from_traces(raw_traces)
    contexts = extract_all_game_contexts(dataset, min_schema_version=args.min_schema_version)

    if args.models:
        model_set = set(args.models)
        before = len(contexts)
        contexts = [c for c in contexts if c.model_a in model_set and c.model_b in model_set]
        log.info("Model filter: %d -> %d games", before, len(contexts))

    if args.run_ids:
        run_id_prefixes = args.run_ids
        before = len(contexts)
        game_run_ids = {g.game_id: g.config.get("experiment_run_id", "") for g in dataset.games}
        contexts = [
            c for c in contexts
            if any(game_run_ids.get(c.game_id, "").startswith(rid) for rid in run_id_prefixes)
        ]
        log.info("Run ID filter %s: %d -> %d games", run_id_prefixes, before, len(contexts))

    if args.exclude_interventions:
        before = len(contexts)
        contexts = [c for c in contexts if not _has_intervention(c, dataset)]
        log.info("Intervention filter: %d -> %d games", before, len(contexts))

    # Sort by game_id for deterministic sharding
    contexts.sort(key=lambda c: c.game_id)

    # Apply shard
    if args.shard:
        shard_idx, shard_total = map(int, args.shard.split("/"))
        before = len(contexts)
        contexts = [c for i, c in enumerate(contexts) if i % shard_total == shard_idx]
        log.info("Shard %d/%d: %d -> %d games", shard_idx, shard_total, before, len(contexts))

    if args.limit:
        contexts = contexts[:args.limit]

    # Build thinking_config if requested
    _THINKING_BUDGETS = {"low": 1024, "medium": 8192, "high": 24576}
    thinking_config = None
    if args.thinking_level:
        if api_format != "anthropic":
            log.warning(
                "--thinking-level is only supported for Anthropic models. "
                "Ignoring for api_format=%s (model thinks by default if supported).",
                api_format,
            )
        else:
            thinking_config = {"thinking_budget": _THINKING_BUDGETS[args.thinking_level]}
            log.info("Thinking mode: %s (budget=%d tokens)", args.thinking_level, thinking_config["thinking_budget"])

    total_rounds = sum(len(c.rounds) for c in contexts)
    log.info("Ready to judge: %d games (%d rounds)", len(contexts), total_rounds)

    if args.dry_run:
        for ctx in contexts:
            log.info("  %s: %d rounds | %s vs %s | %s", ctx.game_id[:12], len(ctx.rounds), ctx.model_a, ctx.model_b, ctx.mode)
        log.info("Dry run complete. %d games would be judged.", len(contexts))
        return

    # Check Firestore for already-completed games (resume support)
    # completed: {game_id: {prompt_version, ...}} — skip only if current prompt already judged
    completed = list_completed_game_ids()
    already_judged = sum(1 for versions in completed.values() if prompt_version in versions)
    log.info("Found %d games in Firestore (%d already judged with prompt %s)",
             len(completed), already_judged, prompt_version)

    # Prepare local output dir
    output_dir = args.output_dir
    (output_dir / "raw").mkdir(parents=True, exist_ok=True)

    # Also check local raw/ dir for already-saved judgments (fallback when Firestore is unavailable)
    local_raw_dir = output_dir / "raw"
    local_done = {p.stem for p in local_raw_dir.glob("*.json")} if local_raw_dir.exists() else set()
    if local_done:
        log.info("Found %d locally saved judgments in %s", len(local_done), local_raw_dir)

    # Judge loop
    pending = [
        ctx for ctx in contexts
        if prompt_version not in completed.get(ctx.game_id, set())
        and ctx.game_id not in local_done
    ]
    skip_count = len(contexts) - len(pending)
    if skip_count:
        log.info("Skipping %d already-completed games", skip_count)

    def judge_one(ctx):
        log.info("Judging %s (%d rounds, %s vs %s, %s mc=%s)...",
                 ctx.game_id[:12], len(ctx.rounds),
                 ctx.model_a, ctx.model_b, ctx.mode, ctx.mc_ratio)
        judgment = judge_game(
            ctx=ctx,
            api_format=api_format,
            api_base=api_base,
            api_key=api_key,
            model=model,
            temperature=args.temperature,
            thinking_config=thinking_config,
        )
        judgment_dict = judgment.model_dump()
        input_transcript = build_judge_user_prompt(ctx)
        save_judgment(ctx.game_id, judgment_dict, judge_model=model, prompt_version=prompt_version,
                      input_transcript=input_transcript)
        local_path = output_dir / "raw" / f"{ctx.game_id}.json"
        with open(local_path, "w") as f:
            json.dump(judgment_dict, f, indent=2)
        total_patterns = sum(len(r.patterns) for r in judgment.rounds)
        log.info("  -> %s: %d rounds, %d patterns (saved)", ctx.game_id[:12], len(judgment.rounds), total_patterns)
        return judgment

    total_pending = len(pending)
    _progress_lock = threading.Lock()
    _counts = {"done": 0, "failed": 0}

    def _on_result(ctx, judgment):
        with _progress_lock:
            _counts["done"] += 1
            done, failed = _counts["done"], _counts["failed"]
        log.info("Progress: %d/%d done, %d failed", done, total_pending, failed)

    def _on_error(ctx, e):
        with _progress_lock:
            _counts["failed"] += 1
            done, failed = _counts["done"], _counts["failed"]
        log.error(
            "FAILED %s [TOKEN BUDGET EXCEEDED — increase JUDGE_MAX_TOKENS]: %s" if isinstance(e, TokenBudgetExceeded)
            else "FAILED %s: %s",
            ctx.game_id, e,
        )
        log.info("Progress: %d/%d done, %d failed", done, total_pending, failed)

    log.info("Judging %d games with parallelism=%d", total_pending, args.max_parallelism)
    results, errors = run_with_parallelism(
        fn=judge_one,
        items=pending,
        max_workers=args.max_parallelism,
        on_result=_on_result,
        on_error=_on_error,
    )
    new_count = len(results)
    fail_count = len(errors)

    # Summary
    print(f"\n{'='*60}")
    print("DISTRIBUTED PHASE 1 COMPLETE")
    print(f"{'='*60}")
    if args.shard:
        print(f"Shard: {args.shard}")
    print(f"Judge model: {model}")
    print(f"New: {new_count} | Skipped: {skip_count} | Failed: {fail_count}")
    print(f"Total in Firestore: {len(completed) + new_count}")

    if errors:
        print(f"\nFailed traces requiring re-processing ({len(errors)}):")
        budget_exceeded = [(ctx, e) for ctx, e in errors if isinstance(e, TokenBudgetExceeded)]
        other_errors = [(ctx, e) for ctx, e in errors if not isinstance(e, TokenBudgetExceeded)]
        if budget_exceeded:
            print(f"  [TOKEN BUDGET EXCEEDED — increase JUDGE_MAX_TOKENS before retrying]")
            for ctx, e in budget_exceeded:
                print(f"    {ctx.game_id}  ({ctx.model_a} vs {ctx.model_b}, {len(ctx.rounds)} rounds)")
        if other_errors:
            print(f"  [Other errors]")
            for ctx, e in other_errors:
                print(f"    {ctx.game_id}  {type(e).__name__}: {e}")

    print(f"{'='*60}")


if __name__ == "__main__":
    main()
