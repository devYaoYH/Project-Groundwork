"""Phase 2: Taxonomy consolidation — cluster free-text pattern names into canonical categories."""

import json
import logging
import time
import urllib.error
from collections import Counter
from pathlib import Path

from json_repair import repair_json

from negotiation_game.backend.defaults import API_MAX_RETRIES, API_BACKOFF_BASE, API_BACKOFF_MAX
from negotiation_game.backend.agents.api import call_llm_streaming
from negotiation_judge.schema import GameJudgment, RoundJudgment, Taxonomy, CanonicalPattern
from negotiation_judge.prompts import build_consolidation_prompt, build_alias_mapping_prompt
from negotiation_judge.judge import TokenBudgetExceeded

log = logging.getLogger("judge.consolidate")

DESCRIPTION_FREQUENCY_THRESHOLD = 3  # include description text only for patterns seen >= this many times


def _flatten_rounds(judgments) -> list[RoundJudgment]:
    """Accept either GameJudgments or RoundJudgments; return a flat list of RoundJudgments."""
    rounds: list[RoundJudgment] = []
    for j in judgments:
        if isinstance(j, GameJudgment):
            rounds.extend(j.rounds)
        else:
            rounds.append(j)
    return rounds


def collect_patterns(judgments) -> list[dict]:
    """Collect all unique pattern names from all judgments.

    Accepts either a list of RoundJudgment or a list of GameJudgment.
    Returns all names sorted alphabetically with their occurrence count.
    Descriptions are included for patterns appearing >= DESCRIPTION_FREQUENCY_THRESHOLD times;
    rare patterns include name only (the consolidation model needs to map every name).
    """
    round_judgments = _flatten_rounds(judgments)

    name_counts: Counter = Counter()
    name_descriptions: dict[str, Counter] = {}

    for j in round_judgments:
        for p in j.patterns:
            name_counts[p.name] += 1
            if p.name not in name_descriptions:
                name_descriptions[p.name] = Counter()
            name_descriptions[p.name][p.description] += 1

    # Sort alphabetically so snake_case grouping is visually apparent
    sorted_names = sorted(name_counts.keys())

    patterns = []
    for name in sorted_names:
        count = name_counts[name]
        entry: dict = {"name": name, "count": count}
        if count >= DESCRIPTION_FREQUENCY_THRESHOLD:
            entry["description"] = name_descriptions[name].most_common(1)[0][0]
        patterns.append(entry)

    log.info("Collected %d unique pattern names (%d total occurrences), all included for consolidation",
             len(name_counts), sum(name_counts.values()))
    return patterns


def _parse_json(raw_text: str) -> dict:
    """Strip markdown fences and parse JSON, with local repair fallback."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        import re
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", cleaned, re.DOTALL)
        if m:
            cleaned = m.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as original_err:
        try:
            repaired = repair_json(cleaned, return_objects=True)
            if isinstance(repaired, dict):
                log.warning("JSON repaired locally (original error: %s)", original_err)
                return repaired
        except Exception:
            pass
        raise original_err


def consolidate_taxonomy(
    judgments,
    api_format: str,
    api_base: str,
    api_key: str,
    model: str,
    temperature: float = 0.0,
    thinking_config: dict | None = None,
    output_path: Path | None = None,
) -> Taxonomy:
    """Run the two-step taxonomy consolidation pass over all judgments.

    Step 1: LLM defines canonical categories (id/label/description) — no aliases.
    Step 2: LLM maps every raw pattern name to a canonical_id.

    Separating the steps means a Step 2 failure is recoverable from the saved
    Step 1 taxonomy without re-running the category-definition call.

    Accepts either GameJudgment or RoundJudgment lists.
    Returns a validated Taxonomy with aliases populated from Step 2.
    """
    patterns = collect_patterns(judgments)
    if not patterns:
        log.warning("No patterns to consolidate")
        return Taxonomy(version=1, negative_patterns=[], positive_patterns=[])

    call_kwargs = dict(
        api_format=api_format,
        api_base=api_base,
        api_key=api_key,
        model=model,
        temperature=temperature,
        thinking_config=thinking_config,
    )

    # --- Step 1: Define canonical categories ---
    log.info("Step 1: defining canonical taxonomy categories from %d pattern names...", len(patterns))
    step1_messages = build_consolidation_prompt(patterns)
    step1_raw = _call_with_backoff(messages=step1_messages, **call_kwargs)
    step1_data = _parse_json(step1_raw)

    # Enforce per-bucket cap
    for bucket in ("negative_patterns", "positive_patterns"):
        if bucket in step1_data and len(step1_data[bucket]) > 10:
            log.warning("Truncating %s from %d to 10", bucket, len(step1_data[bucket]))
            step1_data[bucket] = step1_data[bucket][:10]
        # Ensure aliases field is absent or empty — populated in Step 2
        for cat in step1_data.get(bucket, []):
            cat.pop("aliases", None)

    taxonomy = Taxonomy.model_validate(step1_data)
    log.info(
        "Step 1 complete: %d failure modes + %d positive patterns",
        len(taxonomy.negative_patterns), len(taxonomy.positive_patterns),
    )
    if output_path:
        save_taxonomy(taxonomy, output_path)
        log.info("Step 1 taxonomy saved to %s (no aliases yet — Step 2 pending)", output_path)

    # --- Step 2: Map every raw name to a canonical_id ---
    all_categories = [
        {"id": p.id, "label": p.label, "description": p.description}
        for p in taxonomy.negative_patterns + taxonomy.positive_patterns
    ]
    log.info("Step 2: mapping %d pattern names to %d categories...", len(patterns), len(all_categories))
    step2_messages = build_alias_mapping_prompt(all_categories, patterns)
    step2_raw = _call_with_backoff(messages=step2_messages, **call_kwargs)
    step2_data = _parse_json(step2_raw)

    mappings: dict[str, str] = step2_data.get("mappings", {})
    unmapped = [p["name"] for p in patterns if p["name"] not in mappings]
    if unmapped:
        log.warning("Step 2: %d pattern names not in mapping output: %s", len(unmapped), unmapped[:10])

    # Build alias lists per canonical_id
    alias_index: dict[str, list[str]] = {p.id: [] for p in taxonomy.negative_patterns + taxonomy.positive_patterns}
    other_names: list[str] = []
    for name, canonical_id in mappings.items():
        if canonical_id in alias_index:
            alias_index[canonical_id].append(name)
        else:
            other_names.append(name)

    if other_names:
        log.warning("Step 2: %d names mapped to unknown/other canonical_id: %s", len(other_names), other_names[:10])

    # Populate aliases on taxonomy objects
    for pat in taxonomy.negative_patterns + taxonomy.positive_patterns:
        pat.aliases = sorted(alias_index.get(pat.id, []))

    total_mapped = sum(len(a) for a in alias_index.values())
    log.info(
        "Step 2 complete: %d/%d names mapped to canonical categories (%d unmapped/other)",
        total_mapped, len(patterns), len(unmapped) + len(other_names),
    )
    return taxonomy


def save_taxonomy(taxonomy: Taxonomy, path: Path) -> None:
    """Save taxonomy to JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(taxonomy.model_dump(), f, indent=2)
    log.info("Saved taxonomy to %s", path)


def load_taxonomy(path: Path) -> Taxonomy:
    """Load taxonomy from JSON file."""
    with open(path) as f:
        data = json.load(f)
    return Taxonomy.model_validate(data)


CONSOLIDATION_TIMEOUT_S = 600  # 10 min — Step 2 maps 2400+ names and can be slow


def _call_with_backoff(
    api_format: str,
    api_base: str,
    api_key: str,
    model: str,
    messages: list[dict],
    temperature: float,
    thinking_config: dict | None = None,
) -> str:
    """Call LLM with exponential backoff for transient and timeout errors."""
    for attempt in range(API_MAX_RETRIES):
        try:
            result = call_llm_streaming(
                api_format=api_format,
                api_base=api_base,
                api_key=api_key,
                model=model,
                messages=messages,
                max_tokens=65536,
                temperature=temperature,
                thinking_config=thinking_config,
                timeout=CONSOLIDATION_TIMEOUT_S,
            )
            finish_reason = result.get("finish_reason")
            if finish_reason in ("length", "max_tokens"):
                raise TokenBudgetExceeded(
                    f"Consolidation response truncated (finish_reason={finish_reason!r}, "
                    f"tokens={result.get('total_tokens')}, chars={len(result.get('text', ''))}). "
                    f"Increase max_tokens in consolidate.py."
                )
            if result.get("reasoning"):
                log.info("  [thinking summary] %s", result["reasoning"][:500])
            return result["text"]
        except (urllib.error.HTTPError, TimeoutError, OSError) as e:
            retryable = (
                isinstance(e, urllib.error.HTTPError) and e.code in (429, 500, 502, 503, 529)
            ) or isinstance(e, (TimeoutError, OSError))
            if retryable and attempt < API_MAX_RETRIES - 1:
                wait = min(API_BACKOFF_BASE * (2 ** attempt), API_BACKOFF_MAX)
                log.warning("Retryable error (%s), retrying in %.1fs (attempt %d/%d)",
                            e, wait, attempt + 1, API_MAX_RETRIES)
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("Unreachable")
