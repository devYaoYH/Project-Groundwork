"""RQ19 — Post-Game Reflection Sampler

Collects post-game reflections from V7+ games and writes one text file per model,
each containing a balanced sample of reflections from high-efficiency and
low-efficiency games. Intended as input for manual Opus-assisted synthesis into
system-prompt learnings.

Output files: data/reflections/{model_slug}.txt
Each file contains up to --per-model reflections, split evenly between
high-efficiency (≥ HIGH_THRESHOLD) and low-efficiency (< LOW_THRESHOLD) games.

Usage:
    uv run python -m scripts.analysis.rq19_reflection_sampler
    uv run python -m scripts.analysis.rq19_reflection_sampler --per-model 30 --no-share-projects --no-tom
"""

import argparse
import random
import re
from pathlib import Path

from negotiation_analysis.data_loader import add_common_args, load_dataset_from_args

HIGH_THRESHOLD = 0.90  # joint efficiency considered "high"
LOW_THRESHOLD = 0.50   # joint efficiency considered "low"
DEFAULT_PER_MODEL = 50
OUTPUT_DIR = Path(__file__).parent.parent.parent / "data" / "reflections"


def _model_slug(model: str) -> str:
    """Filesystem-safe model name."""
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", model).strip("_")


def _efficiency_bucket(eff: float) -> str:
    if eff >= HIGH_THRESHOLD:
        return "high"
    if eff < LOW_THRESHOLD:
        return "low"
    return "mid"


def collect_reflections(dataset, per_model: int, seed: int = 42) -> dict[str, dict[str, list[str]]]:
    """Return {model: {"high": [...], "low": [...]}} with reflection strings."""
    rng = random.Random(seed)
    buckets: dict[str, dict[str, list[str]]] = {}

    for g in dataset.games:
        reflections = g.result.get("reflections", {})
        if not reflections:
            continue

        # Game-level mean efficiency across rounds
        effs = [r.joint_efficiency for r in g.rounds if r.joint_efficiency == r.joint_efficiency]
        if not effs:
            continue
        mean_eff = sum(effs) / len(effs)
        bucket = _efficiency_bucket(mean_eff)
        if bucket == "mid":
            continue  # only collect clear high / low signal

        for agent_id, model in [("agent_a", g.model_a), ("agent_b", g.model_b)]:
            text = (reflections.get(agent_id) or "").strip()
            if not text:
                continue

            if model not in buckets:
                buckets[model] = {"high": [], "low": []}
            buckets[model][bucket].append(
                f"[game={g.game_id[:8]} mode={g.mode} mc={g.metadata.get('mc_bucket','?')} "
                f"eff={mean_eff:.2f} agent={agent_id}]\n{text}"
            )

    # Shuffle so the sample is not experiment-order biased
    for model in buckets:
        for b in ("high", "low"):
            rng.shuffle(buckets[model][b])

    return buckets


def sample_and_write(buckets: dict, per_model: int, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    half = per_model // 2

    for model, sides in sorted(buckets.items()):
        high_sample = sides["high"][:half]
        low_sample = sides["low"][:half]

        # If one side is short, fill up from the other
        shortfall_high = half - len(high_sample)
        shortfall_low  = half - len(low_sample)
        if shortfall_high > 0:
            high_sample += sides["low"][half: half + shortfall_high]
        if shortfall_low > 0:
            low_sample  += sides["high"][half: half + shortfall_low]

        combined = high_sample + low_sample
        if not combined:
            continue

        slug = _model_slug(model)
        out_path = output_dir / f"{slug}.txt"

        header = (
            f"Model: {model}\n"
            f"Total reflections collected: {len(sides['high'])} high-eff / {len(sides['low'])} low-eff\n"
            f"Sampled: {len(high_sample)} high + {len(low_sample)} low = {len(combined)} total\n"
            f"High-efficiency threshold: ≥{HIGH_THRESHOLD:.0%}  |  Low: <{LOW_THRESHOLD:.0%}\n"
            + "=" * 72 + "\n\n"
        )

        sections = []
        if high_sample:
            sections.append(
                "── HIGH-EFFICIENCY GAMES (" + str(len(high_sample)) + ") ──\n\n"
                + "\n\n---\n\n".join(high_sample)
            )
        if low_sample:
            sections.append(
                "── LOW-EFFICIENCY GAMES (" + str(len(low_sample)) + ") ──\n\n"
                + "\n\n---\n\n".join(low_sample)
            )

        out_path.write_text(header + "\n\n".join(sections) + "\n", encoding="utf-8")
        print(f"  {slug}.txt  ({len(combined)} reflections)")


def print_summary(buckets: dict) -> None:
    print(f"\n{'Model':<45}  {'High':>6}  {'Low':>6}  {'Total':>7}")
    print("-" * 68)
    for model in sorted(buckets):
        h = len(buckets[model]["high"])
        lo = len(buckets[model]["low"])
        print(f"  {model:<43}  {h:>6}  {lo:>6}  {h+lo:>7}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RQ19: Post-game reflection sampler")
    add_common_args(parser)
    parser.add_argument(
        "--per-model", type=int, default=DEFAULT_PER_MODEL,
        help=f"Max reflections per model output file (default: {DEFAULT_PER_MODEL})",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for sampling (default: 42)",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=OUTPUT_DIR,
        help=f"Directory to write reflection files (default: {OUTPUT_DIR})",
    )
    args = parser.parse_args()

    dataset = load_dataset_from_args(args)
    print(f"\nCollecting reflections from {len(dataset.games)} games...")
    buckets = collect_reflections(dataset, per_model=args.per_model, seed=args.seed)

    print_summary(buckets)

    print(f"\nWriting to {args.output_dir}/")
    sample_and_write(buckets, per_model=args.per_model, output_dir=args.output_dir)
    print("\nDone.")
