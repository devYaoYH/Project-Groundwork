"""Judge pattern prevalence comparison: main cohort vs full-transparency subset.

Combines the canonical taxonomy alias index with the transparency-specific label
mappings to compute LLM-judge pattern rates for two 120-environment subsets of the
Qwen 3.5 Flash x GPT-5 Mini pair:
  - Baseline:          main 720-environment cohort cross-play slice (run 6cb004cb)
  - Full-transparency: both agents receive full project info upfront (runs 56abe7a8,
                       ac21edea), covering all 3 MC ratios x 2 stability x 2 rotation

LIMITATIONS
-----------
- Results are directional only. The judge (MiniMax M2.5 via OpenRouter) has no
  human-annotated ground truth; pattern labels carry exploratory, not confirmatory,
  weight. Do not report absolute rates as established facts.
- The transparency subset uses a separate label mapping file
  (taxonomy_full_transparency.json, produced by judge/map_transparency_labels.py via
  Gemini 3.1 Pro) to resolve the ~283 free-text pattern names that fell outside the
  main taxonomy's alias list. These secondary mappings introduce additional noise.
- Small N per condition cell (e.g. 10 games per MC x stability x rotation bucket)
  makes per-condition breakdowns unreliable; only aggregate rates are reported here.

PREREQUISITES
-------------
The following files must exist before running this script:
  1. data/experiment_traces.json
       Primary environment cache. Refresh with: uv run python scripts/cache_experiment_data.py
  2. judge/output/raw/<episode_uid>.json  (one per environment)
       Raw MiniMax judge outputs. Produce with:
         uv run python -m judge.run_distributed \
           --provider openrouter --model minimax/minimax-m2.5 \
           --run-ids "56abe7a8" "ac21edea" --cache
       The 720-environment baseline raw files are already present from the main judge run.
  3. judge/output/taxonomy.json
       Canonical 16-pattern taxonomy. Produced during judge consolidation.
  4. judge/output/taxonomy_full_transparency.json
       Transparency-specific alias mappings (283 free-text names -> canonical IDs).
       Produce with: uv run python -m judge.map_transparency_labels
       Requires GOOGLE_API_KEY in .env (calls gemini-3.1-pro-preview).

SIDE EFFECTS
------------
- Read-only. Writes no files; does not modify any judge output or taxonomy files.

Usage:
    uv run python -m scripts.analysis.jq_transparency_judge_patterns
    uv run python -m scripts.analysis.jq_transparency_judge_patterns --latex
"""

from collections import Counter
from pathlib import Path
import json

from negotiation_judge.aggregate import build_alias_index
from negotiation_judge.consolidate import load_taxonomy
from negotiation_analysis.data_loader import load_experiment_data

TRANSPARENCY_RUN_IDS = frozenset({
    "56abe7a8-2d59-4b7b-9d4d-80cbdd078a65",
    "ac21edea-41ae-4953-894c-0327927b0e8a",
})
BASELINE_PAIR_RUN_ID = "6cb004cb-1097-4c87-9003-679a41343733"

RAW_DIR = Path(__file__).parents[2] / "judge" / "output" / "raw"
TAXONOMY_PATH = Path(__file__).parents[2] / "judge" / "output" / "taxonomy.json"
TRANSPARENCY_TAXONOMY_PATH = Path(__file__).parents[2] / "judge" / "output" / "taxonomy_full_transparency.json"


def _normalize(s: str) -> str:
    return " ".join(s.lower().split()) if s else ""


def build_combined_lookup(taxonomy, transp_taxonomy_path: Path) -> dict[str, str]:
    """Return {normalized_name: canonical_id} combining main aliases + transparency mappings."""
    alias_index = build_alias_index(taxonomy)
    lookup: dict[str, str] = {k: v[0] for k, v in alias_index.items()}

    if transp_taxonomy_path.exists():
        with open(transp_taxonomy_path) as f:
            transp = json.load(f)
        for name, entry in transp["mappings"].items():
            cid = entry["canonical_id"]
            if cid != "other":
                norm = _normalize(name)
                lookup.setdefault(norm, cid)

    return lookup


def compute_pattern_rates(
    episode_uids: set[str],
    lookup: dict[str, str],
    taxonomy,
) -> dict:
    """Compute per-pattern rates from raw judge output for a set of environment IDs.

    Returns dict with keys:
        total_rounds, suboptimal_rounds, optimal_rounds,
        neg_counts (Counter canonical_id -> instances in suboptimal rounds),
        pos_counts (Counter canonical_id -> instances in optimal rounds),
    """
    total_sub, total_opt = 0, 0
    neg_counts: Counter = Counter()
    pos_counts: Counter = Counter()

    for gid in episode_uids:
        path = RAW_DIR / f"{gid}.json"
        if not path.exists():
            continue
        with open(path) as f:
            d = json.load(f)
        for r in d.get("rounds", []):
            outcome = r.get("round_outcome", "")
            is_sub = outcome in ("suboptimal", "overdrawn")
            if is_sub:
                total_sub += 1
            else:
                total_opt += 1
            for p in r.get("patterns", []):
                cid = lookup.get(_normalize(p.get("name", "")), "other")
                if cid == "other":
                    continue
                if is_sub:
                    neg_counts[cid] += 1
                else:
                    pos_counts[cid] += 1

    return {
        "total_rounds": total_sub + total_opt,
        "suboptimal_rounds": total_sub,
        "optimal_rounds": total_opt,
        "neg_counts": neg_counts,
        "pos_counts": pos_counts,
    }


def analyze() -> dict:
    taxonomy = load_taxonomy(TAXONOMY_PATH)
    lookup = build_combined_lookup(taxonomy, TRANSPARENCY_TAXONOMY_PATH)

    games = load_experiment_data()
    transp_ids = {
        g["episode_uid"] for g in games
        if g.get("episode_id") in TRANSPARENCY_RUN_IDS
    }
    baseline_ids = {
        g["episode_uid"] for g in games
        if g.get("episode_id") == BASELINE_PAIR_RUN_ID
        and (
            "gpt5m-qwen" in g.get("experiment_label", "")
            or "qwen-gpt5m" in g.get("experiment_label", "")
        )
        and "transparency" not in g.get("experiment_label", "")
    }

    baseline = compute_pattern_rates(baseline_ids, lookup, taxonomy)
    transparency = compute_pattern_rates(transp_ids, lookup, taxonomy)

    # Build comparison rows
    neg_rows = []
    for p in taxonomy.negative_patterns:
        b_rate = baseline["neg_counts"].get(p.id, 0) / baseline["suboptimal_rounds"] * 100
        t_rate = transparency["neg_counts"].get(p.id, 0) / transparency["suboptimal_rounds"] * 100
        neg_rows.append({
            "canonical_id": p.id,
            "label": p.label,
            "baseline_pct": round(b_rate, 1),
            "transparency_pct": round(t_rate, 1),
            "delta_pp": round(t_rate - b_rate, 1),
        })

    pos_rows = []
    for p in taxonomy.positive_patterns:
        b_rate = baseline["pos_counts"].get(p.id, 0) / baseline["optimal_rounds"] * 100
        t_rate = transparency["pos_counts"].get(p.id, 0) / transparency["optimal_rounds"] * 100
        pos_rows.append({
            "canonical_id": p.id,
            "label": p.label,
            "baseline_pct": round(b_rate, 1),
            "transparency_pct": round(t_rate, 1),
            "delta_pp": round(t_rate - b_rate, 1),
        })

    return {
        "taxonomy": taxonomy,
        "baseline_meta": {
            "n_games": len(baseline_ids),
            "suboptimal_rounds": baseline["suboptimal_rounds"],
            "optimal_rounds": baseline["optimal_rounds"],
        },
        "transparency_meta": {
            "n_games": len(transp_ids),
            "suboptimal_rounds": transparency["suboptimal_rounds"],
            "optimal_rounds": transparency["optimal_rounds"],
        },
        "neg_rows": neg_rows,
        "pos_rows": pos_rows,
    }


def print_summary(results: dict) -> None:
    bm = results["baseline_meta"]
    tm = results["transparency_meta"]
    print("=" * 72)
    print("Judge Pattern Comparison: Baseline vs Full-Transparency (directional)")
    print("=" * 72)
    print(f"Baseline:     N={bm['n_games']} games | {bm['suboptimal_rounds']} suboptimal, {bm['optimal_rounds']} optimal rounds")
    print(f"Transparent:  N={tm['n_games']} games | {tm['suboptimal_rounds']} suboptimal, {tm['optimal_rounds']} optimal rounds")
    print()

    header = f"  {'Pattern':<38} {'Baseline':>9} {'Transparent':>12} {'Delta':>8}"
    print("Negative patterns (% of suboptimal rounds):")
    print(header)
    print("  " + "-" * 70)
    for row in results["neg_rows"]:
        print(f"  {row['label']:<38} {row['baseline_pct']:>8.1f}% {row['transparency_pct']:>11.1f}%  {row['delta_pp']:>+7.1f} pp")

    print()
    print("Positive patterns (% of optimal rounds):")
    print(header)
    print("  " + "-" * 70)
    for row in results["pos_rows"]:
        print(f"  {row['label']:<38} {row['baseline_pct']:>8.1f}% {row['transparency_pct']:>11.1f}%  {row['delta_pp']:>+7.1f} pp")

    print()
    print("Note: rates are directional only; judge labels lack human-aligned ground truth.")


def print_latex(results: dict) -> None:
    bm = results["baseline_meta"]
    tm = results["transparency_meta"]

    print(r"\begin{table}[htbp]")
    print(r"\centering")
    print(r"\small")
    print(r"\caption{LLM judge pattern rates for the Qwen~3.5~Flash $\times$ GPT-5~Mini pair:")
    print(r"  baseline ($N{=}120$) vs.\ full-transparency ($N{=}120$).")
    print(r"  Rates are \emph{directional only} --- the judge lacks a human-annotated ground truth.}")
    print(r"\label{tab:transparency_judge}")
    print(r"\begin{tabular}{l r r r}")
    print(r"\toprule")
    print(r"\textbf{Pattern} & \textbf{Baseline} & \textbf{Transparent} & \textbf{$\Delta$} \\")
    print(r"\midrule")
    print(r"\multicolumn{4}{l}{\emph{Negative patterns --- \% of suboptimal rounds"
          f" (baseline: {bm['suboptimal_rounds']}, transparent: {tm['suboptimal_rounds']})}} \\\\[2pt]")
    for row in results["neg_rows"]:
        delta_str = f"{row['delta_pp']:+.1f}"
        bold = r"\textbf{" if abs(row["delta_pp"]) >= 10 else ""
        endbold = r"}" if bold else ""
        print(f"{bold}{row['label']}{endbold} & {row['baseline_pct']:.1f} & {row['transparency_pct']:.1f} & {delta_str} \\\\")
    print(r"\midrule")
    print(r"\multicolumn{4}{l}{\emph{Positive patterns --- \% of optimal rounds"
          f" (baseline: {bm['optimal_rounds']}, transparent: {tm['optimal_rounds']})}} \\\\[2pt]")
    for row in results["pos_rows"]:
        delta_str = f"{row['delta_pp']:+.1f}"
        bold = r"\textbf{" if abs(row["delta_pp"]) >= 10 else ""
        endbold = r"}" if bold else ""
        print(f"{bold}{row['label']}{endbold} & {row['baseline_pct']:.1f} & {row['transparency_pct']:.1f} & {delta_str} \\\\")
    print(r"\bottomrule")
    print(r"\end{tabular}")
    print(r"\end{table}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--latex", action="store_true", help="Print LaTeX table instead of text summary")
    args = parser.parse_args()

    results = analyze()
    if args.latex:
        print_latex(results)
    else:
        print_summary(results)
