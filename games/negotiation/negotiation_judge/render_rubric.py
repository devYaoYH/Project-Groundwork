"""Render taxonomy_v2.json (with rubric fields) into a human-readable markdown doc.

Usage:
    uv run python -m judge.render_rubric
"""

import json

from negotiation_game.backend.defaults import REPO_ROOT

INPUT_PATH = REPO_ROOT / "judge" / "output" / "taxonomy_v2.json"
OUTPUT_PATH = REPO_ROOT / "judge" / "output" / "taxonomy_v2.md"


def _render_category(cat: dict) -> str:
    lines = [f"### `{cat['id']}` — {cat['label']}", ""]
    lines.append(f"**Definition.** {cat.get('definition', cat.get('description', ''))}")
    lines.append("")
    lines.append("**Inclusion criteria** (all must hold):")
    for c in cat.get("inclusion_criteria", []):
        lines.append(f"- {c}")
    lines.append("")
    lines.append("**Exclusion criteria** (NOT this category if...):")
    for c in cat.get("exclusion_criteria", []):
        lines.append(f"- {c}")
    lines.append("")
    lines.append(f"**Example.** {cat.get('example', '_(none)_')}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    with open(INPUT_PATH) as f:
        tax = json.load(f)

    out = [
        "# Multi-Agent Coordination Pattern Rubric (taxonomy_v2)",
        "",
        "Domain-neutral classification rubric for multi-agent coordination episodes.",
        "Intended for both LLM and human raters. Classification is **outcome-agnostic** —",
        "a positive pattern can appear in a failed round, a negative in an optimal round.",
        "Stratify by outcome at the reporting layer, not at classification.",
        "",
        "Rules:",
        "- Apply **all** inclusion criteria (AND-logic) before assigning a category.",
        "- If a trace matches multiple categories, consult each category's **exclusion criteria**",
        "  to pick the most precise fit. Tie-breakers between near-duplicates are called out explicitly.",
        "- A single round may carry multiple patterns (one per distinct behavior observed).",
        "",
        "---",
        "",
        f"## Negative Patterns ({len(tax['negative_patterns'])})",
        "",
    ]
    for cat in tax["negative_patterns"]:
        out.append(_render_category(cat))
        out.append("---")
        out.append("")

    out.append(f"## Positive Patterns ({len(tax['positive_patterns'])})")
    out.append("")
    for cat in tax["positive_patterns"]:
        out.append(_render_category(cat))
        out.append("---")
        out.append("")

    OUTPUT_PATH.write_text("\n".join(out), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH} ({OUTPUT_PATH.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
