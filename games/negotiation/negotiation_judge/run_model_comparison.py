"""Test the judge on a single environment with multiple models.

Outputs one JSON per model to judge/output/model_comparison/<model_slug>.json

Usage:
    uv run python -m judge.run_model_comparison --environment-id 40ae9f36
"""

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from negotiation_game.backend.defaults import REPO_ROOT
from negotiation_analysis.data_loader import load_experiment_data
from negotiation_analysis.models import NegotiationDataset
from negotiation_judge.extractor import extract_all_game_contexts
from negotiation_judge.judge import judge_game
from negotiation_judge.schema import JudgeGameContext

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-20s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("judge.model_comparison")

MODELS = [
    {
        "slug": "minimax-m2.5",
        "model": "minimax/minimax-m2.5:free",
        "provider": "openrouter",
    },
    {
        "slug": "nemotron-120b",
        "model": "nvidia/nemotron-3-super-120b-a12b:free",
        "provider": "openrouter",
    },
    {
        "slug": "glm-4.7",
        "model": "z-ai/glm-4.7",
        "provider": "openrouter",
    },
    {
        "slug": "gemini-2-flash",
        "model": "google/gemini-2.0-flash-001",
        "provider": "openrouter",
    },
]

PROVIDERS = {
    "openrouter": {
        "api_format": "openai",
        "api_base": "https://openrouter.ai/api/v1",
        "key_env": "OPENROUTER_API_KEY",
    },
}


def main():
    parser = argparse.ArgumentParser(description="Run judge on one environment with multiple models")
    parser.add_argument("--environment-id", default="40ae9f36", help="Environment ID (or prefix)")
    parser.add_argument("--output-dir", type=Path,
                        default=REPO_ROOT / "judge" / "output" / "model_comparison")
    parser.add_argument("--temperature", type=float, default=0.0)
    args = parser.parse_args()

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # Load the environment context
    log.info("Loading experiment data...")
    raw_traces = load_experiment_data()
    dataset = NegotiationDataset.from_traces(raw_traces)

    # Find the environment
    matching = [g.episode_uid for g in dataset.games if g.episode_uid.startswith(args.episode_uid)]
    if not matching:
        log.error("No environment found matching '%s'", args.episode_uid)
        sys.exit(1)
    episode_uid = matching[0]

    game_ctxs = extract_all_game_contexts(dataset, episode_uids={episode_uid})
    if not game_ctxs:
        log.error("No eligible environment context for %s", episode_uid)
        sys.exit(1)
    ctx = game_ctxs[0]
    log.info("Environment: %s (%d rounds, %s vs %s, %s)", episode_uid, len(ctx.rounds), ctx.model_a, ctx.model_b, ctx.mode)

    # Run each model
    for m in MODELS:
        slug = m["slug"]
        model_name = m["model"]
        prov = PROVIDERS[m["provider"]]
        api_key = os.environ.get(prov["key_env"])
        if not api_key:
            log.error("Missing %s for %s, skipping", prov["key_env"], slug)
            continue

        out_path = output_dir / f"{slug}.json"
        if out_path.exists():
            log.info("[%s] SKIP (already exists at %s)", slug, out_path)
            continue

        log.info("[%s] Judging with model=%s ...", slug, model_name)
        try:
            judgment = judge_game(
                ctx=ctx,
                api_format=prov["api_format"],
                api_base=prov["api_base"],
                api_key=api_key,
                model=model_name,
                temperature=args.temperature,
            )
            with open(out_path, "w") as f:
                json.dump(judgment.model_dump(), f, indent=2)
            total_patterns = sum(len(r.patterns) for r in judgment.rounds)
            log.info("[%s] Done: %d rounds, %d patterns -> %s", slug, len(judgment.rounds), total_patterns, out_path)
        except Exception as e:
            log.error("[%s] FAILED: %s", slug, e)
            # Save the error for inspection
            with open(output_dir / f"{slug}_error.txt", "w") as f:
                f.write(str(e))

    # Summary
    print(f"\n{'='*60}")
    print("MODEL COMPARISON RESULTS")
    print(f"{'='*60}")
    print(f"Environment: {episode_uid}")
    for m in MODELS:
        out_path = output_dir / f"{m['slug']}.json"
        err_path = output_dir / f"{m['slug']}_error.txt"
        if out_path.exists():
            with open(out_path) as f:
                j = json.load(f)
            total_p = sum(len(r["patterns"]) for r in j["rounds"])
            print(f"  {m['slug']:20s} — {len(j['rounds'])} rounds, {total_p} patterns")
        elif err_path.exists():
            print(f"  {m['slug']:20s} — FAILED (see {err_path})")
        else:
            print(f"  {m['slug']:20s} — not run")
    print(f"Output: {output_dir}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
