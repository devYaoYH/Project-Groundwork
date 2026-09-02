"""Interactive entrypoint: judge a single game and print results.

Usage:
    uv run python -m judge.run_interactive \
        --game-id dbd45fed \
        --provider anthropic
"""

import argparse
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
from negotiation_judge.consolidate import load_taxonomy
from negotiation_judge.schema import GameJudgment, RoundJudgment, Taxonomy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("judge.interactive")


def _print_round(j: RoundJudgment, taxonomy: Taxonomy | None = None):
    """Pretty-print a single round judgment."""
    alias_map: dict[str, str] = {}
    if taxonomy:
        for cp in taxonomy.canonical_patterns:
            for alias in cp.aliases:
                alias_map[alias.lower()] = f"{cp.label} ({cp.id})"

    outcome_icon = {"optimal": "\u2705", "suboptimal": "\u26a0\ufe0f", "overdrawn": "\u274c"}
    icon = outcome_icon.get(j.round_outcome.value, "")

    print(f"\n{'='*60}")
    print(f"Round {j.round_number} {icon} {j.round_outcome.value.upper()} | efficiency: {j.joint_efficiency:.0%}")
    print(f"{'='*60}")

    if j.prior_round_influence:
        print(f"  Prior round influence: {j.prior_round_influence}")

    for p in j.patterns:
        eff_icon = {"positive": "+", "negative": "-", "neutral": "~"}
        canonical = alias_map.get(p.name.lower(), "")
        canonical_str = f" -> [{canonical}]" if canonical else ""

        print(f"\n  [{eff_icon.get(p.effectiveness.value, '?')}] {p.name}{canonical_str}")
        print(f"      {p.description}")
        print(f"      intent={p.intent.value}, coherent={p.speech_allocation_coherent}")
        if p.coherence_note:
            print(f"      note: {p.coherence_note}")
        for ev in p.evidence:
            quote = ev.quote[:120] + "..." if len(ev.quote) > 120 else ev.quote
            print(f"      > [{ev.speaker}/{ev.type}] \"{quote}\"")

    print(f"\n  Attribution: {j.round_attribution}")


def _print_game_judgment(j: GameJudgment, taxonomy: Taxonomy | None = None):
    """Pretty-print a whole-game judgment."""
    print(f"\nGame: {j.game_id}")
    print(f"Models: {j.model_a} vs {j.model_b}")
    print(f"Mode: {j.mode} | mc_ratio: {j.mc_ratio}")
    print(f"\nGame-level attribution: {j.game_attribution}")

    for rj in sorted(j.rounds, key=lambda r: r.round_number):
        _print_round(rj, taxonomy)
    print()


def main():
    parser = argparse.ArgumentParser(description="Interactive LLM judge for a single game")
    parser.add_argument("--game-id", required=True, help="Game ID (or prefix)")
    parser.add_argument("--provider", default="anthropic", choices=list(LLM_PROVIDERS.keys()))
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--taxonomy", type=Path, default=None,
                        help="Path to taxonomy.json for canonical mapping")
    args = parser.parse_args()

    # Resolve provider
    provider = LLM_PROVIDERS[args.provider]
    api_format = provider["api_format"]
    api_base = provider["api_base"]
    model = args.model or os.environ.get("JUDGE_MODEL") or provider["model"]

    key_env_map = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }
    key_env = args.api_key_env or key_env_map.get(args.provider, "API_KEY")
    api_key = os.environ.get(key_env)
    if not api_key:
        log.error("Set %s environment variable.", key_env)
        sys.exit(1)

    # Load taxonomy if provided
    taxonomy = None
    if args.taxonomy and args.taxonomy.exists():
        taxonomy = load_taxonomy(args.taxonomy)
        log.info("Loaded taxonomy with %d patterns", len(taxonomy.canonical_patterns))
    else:
        default_path = REPO_ROOT / "judge" / "output" / "taxonomy.json"
        if default_path.exists():
            taxonomy = load_taxonomy(default_path)
            log.info("Loaded taxonomy from default path (%d patterns)", len(taxonomy.canonical_patterns))

    # Load data
    raw_traces = load_experiment_data()
    dataset = NegotiationDataset.from_traces(raw_traces)

    # Find matching game(s)
    matching = [g.game_id for g in dataset.games if g.game_id.startswith(args.game_id)]
    if not matching:
        log.error("No game found matching '%s'", args.game_id)
        sys.exit(1)
    if len(matching) > 1:
        log.warning("Multiple matches for '%s': %s — using first", args.game_id, matching)
    game_id = matching[0]

    # Extract the game context (single call, all rounds)
    game_ctxs = extract_all_game_contexts(dataset, game_ids={game_id})
    if not game_ctxs:
        log.error("No eligible game found for %s", game_id)
        sys.exit(1)
    ctx = game_ctxs[0]

    log.info("Judging game %s (%d rounds) — one LLM call...", game_id, len(ctx.rounds))
    judgment = judge_game(
        ctx=ctx,
        api_format=api_format,
        api_base=api_base,
        api_key=api_key,
        model=model,
        temperature=args.temperature,
    )

    _print_game_judgment(judgment, taxonomy)


if __name__ == "__main__":
    main()
