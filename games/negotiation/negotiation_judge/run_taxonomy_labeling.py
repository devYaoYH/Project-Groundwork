"""Taxonomy labeling: classify each round against a fixed rubric.

For each round in every extracted environment (filtered to the judged set from
judgments.csv), send the full transcript to Gemini (Vertex AI / ADC) and get
back a binary yes/no for each of the 10 taxonomy_v2 labels.

Output: judge/output/taxonomy_labels.csv
  Columns: episode_uid, round_number, <10 label ids as 0/1 columns>

Usage:
    uv run python -m judge.run_taxonomy_labeling [--concurrency 8] [--output PATH]
    uv run python -m judge.run_taxonomy_labeling \
        --taxonomy judge/output/taxonomy_v3.json \
        --sample judge/output/calibration_rounds_v3.json \
        --output judge/output/taxonomy_labels_gemini_v3_calibration.csv
"""

import argparse
import asyncio
import csv
import json
import logging
import re
import time
import urllib.error
from pathlib import Path

from json_repair import repair_json

from negotiation_game.backend.defaults import (
    REPO_ROOT,
    LLM_PROVIDERS,
    API_MAX_RETRIES,
    API_BACKOFF_BASE,
    API_BACKOFF_MAX,
)
from negotiation_game.backend.agents.api import call_llm_oneshot_with_thinking

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-20s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("judge.taxonomy_labeling")

MODEL = "publishers/google/models/gemini-3-flash-preview"
PROVIDER = "gemini_vertexai"
LABELING_MAX_TOKENS = 4096
TEMPERATURE = 0.0
THINKING_BUDGET = 8192
DEFAULT_CONCURRENCY = 8

DEFAULT_TAXONOMY_PATH = REPO_ROOT / "judge" / "output" / "taxonomy_v2.json"
DEFAULT_EXTRACTED_DIR = REPO_ROOT / "judge" / "output" / "extracted"
DEFAULT_JUDGMENTS_CSV = REPO_ROOT / "judge" / "output" / "judgments.csv"
DEFAULT_OUTPUT = REPO_ROOT / "judge" / "output" / "taxonomy_labels.csv"
DEFAULT_THINKING_LOG = REPO_ROOT / "judge" / "output" / "taxonomy_thinking.jsonl"

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


# ---------------------------------------------------------------------------
# Taxonomy loading + prompt construction
# ---------------------------------------------------------------------------

def load_taxonomy(path: Path) -> tuple[list[str], str]:
    """Return (ordered list of label IDs, rubric text for the prompt)."""
    with open(path) as f:
        taxonomy = json.load(f)

    label_ids: list[str] = []
    sections: list[str] = []

    for polarity in ("negative_patterns", "positive_patterns"):
        for p in taxonomy.get(polarity, []):
            label_ids.append(p["id"])
            inc = "\n".join(f"  - {c}" for c in p.get("inclusion_criteria", []))
            exc = "\n".join(f"  - {c}" for c in p.get("exclusion_criteria", []))
            sections.append(
                f"### {p['id']} ({p['label']})\n"
                f"Definition: {p['definition']}\n"
                f"Inclusion (ALL must hold):\n{inc}\n"
                f"Exclusion (NOT this if...):\n{exc}"
            )

    rubric_text = "\n\n".join(sections)
    return label_ids, rubric_text


def build_system_prompt(rubric_text: str, label_ids: list[str]) -> str:
    ids_list = ", ".join(f'"{lid}"' for lid in label_ids)
    example_obj = "{" + ids_list + "}"
    return f"""You are an expert annotator for multi-agent negotiation research.

Given a single negotiation round transcript (including agent thinking, speech, \
final allocations, and outcome), classify which of the following 10 coordination \
patterns are present in that round.

Apply AND-logic for inclusion criteria: ALL criteria must hold before assigning \
a label. A round may carry multiple patterns. Be strict — only label a pattern \
if the evidence is clear from the transcript.

## Rubric

{rubric_text}

## Response format

Respond with ONLY a JSON object mapping each label ID to true or false.
No explanation, no markdown fences. Example:
{example_obj}
Replace each value with true or false based on the round.
"""


def format_round_transcript(rnd: dict) -> str:
    """Format a single round dict into a readable transcript string."""
    lines: list[str] = []
    lines.append(f"=== Round {rnd['round_number']} | Outcome: {rnd.get('round_outcome', '?')} | "
                 f"Joint efficiency: {rnd.get('joint_efficiency', '?'):.2%} ===")

    for turn in rnd.get("cheap_talk", []):
        speaker = turn.get("speaker", "?")
        turn_n = turn.get("turn", "?")
        label = f"[{speaker} turn {turn_n}]"
        if turn.get("is_decision"):
            label += " (decision)"
        if turn.get("thinking"):
            lines.append(f"{label} THINKING: {turn['thinking']}")
        if turn.get("speech"):
            lines.append(f"{label} SPEECH: {turn['speech']}")

    alloc_a = rnd.get("allocation_a") or {}
    alloc_b = rnd.get("allocation_b") or {}
    alloc_a_str = ", ".join(f"{k}={v}" for k, v in alloc_a.items()) or "none"
    alloc_b_str = ", ".join(f"{k}={v}" for k, v in alloc_b.items()) or "none"
    lines.append(f"[Final submission] agent_a: {alloc_a_str} | agent_b: {alloc_b_str}")

    reward_a = rnd.get("reward_a")
    reward_b = rnd.get("reward_b")
    if reward_a is not None or reward_b is not None:
        lines.append(f"[Rewards] agent_a: {reward_a}, agent_b: {reward_b}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# API call with retry
# ---------------------------------------------------------------------------

def _parse_labels(text: str, label_ids: list[str]) -> dict[str, bool] | None:
    """Strip fences and parse JSON, returning {label_id: bool} or None on failure."""
    cleaned = text.strip()
    m = _FENCE_RE.search(cleaned)
    if m:
        cleaned = m.group(1).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        try:
            data = json.loads(repair_json(cleaned))
        except Exception:
            return None

    if not isinstance(data, dict):
        return None

    result: dict[str, bool] = {}
    for lid in label_ids:
        val = data.get(lid)
        if isinstance(val, bool):
            result[lid] = val
        elif isinstance(val, int):
            result[lid] = bool(val)
        elif isinstance(val, str):
            result[lid] = val.lower() in ("true", "yes", "1")
        else:
            result[lid] = False
    return result


def call_with_retry(
    api_format: str,
    api_base: str,
    api_key: str,
    messages: list[dict],
    label_ids: list[str],
) -> tuple[dict[str, bool], str | None]:
    """Call the LLM with exponential backoff. Returns (labels, thinking). Raises RuntimeError after max retries."""
    last_err: Exception | None = None
    for attempt in range(API_MAX_RETRIES):
        try:
            text, thinking = call_llm_oneshot_with_thinking(
                api_format=api_format,
                api_base=api_base,
                api_key=api_key,
                model=MODEL,
                messages=messages,
                max_tokens=LABELING_MAX_TOKENS,
                temperature=TEMPERATURE,
                thinking_budget=THINKING_BUDGET,
            )
            result = _parse_labels(text, label_ids)
            if result is None:
                raise ValueError(f"Unparseable response: {text[:200]}")
            return result, thinking
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code not in (429, 500, 502, 503, 529):
                raise
            wait = min(API_BACKOFF_BASE * (2 ** attempt), API_BACKOFF_MAX)
            log.warning("HTTP %d on attempt %d/%d — sleeping %.1fs", e.code, attempt + 1, API_MAX_RETRIES, wait)
            time.sleep(wait)
        except (TimeoutError, OSError, TypeError, ValueError) as e:
            last_err = e
            wait = min(API_BACKOFF_BASE * (2 ** attempt), API_BACKOFF_MAX)
            log.warning("%s on attempt %d/%d — sleeping %.1fs", e, attempt + 1, API_MAX_RETRIES, wait)
            time.sleep(wait)

    raise RuntimeError(f"Failed after {API_MAX_RETRIES} attempts: {last_err}")


# ---------------------------------------------------------------------------
# Async orchestration
# ---------------------------------------------------------------------------

async def label_round(
    semaphore: asyncio.Semaphore,
    loop: asyncio.AbstractEventLoop,
    executor,
    episode_uid: str,
    rnd: dict,
    system_prompt: str,
    label_ids: list[str],
    api_format: str,
    api_base: str,
    api_key: str,
) -> tuple[dict, str | None]:
    """Label one round; returns (result row dict, thinking text or None)."""
    transcript = format_round_transcript(rnd)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": transcript},
    ]

    thinking = None
    async with semaphore:
        try:
            labels, thinking = await loop.run_in_executor(
                executor,
                lambda: call_with_retry(api_format, api_base, api_key, messages, label_ids),
            )
            log.debug("Labeled %s round %d", episode_uid, rnd["round_number"])
        except Exception as e:
            log.error("FAILED %s round %d: %s", episode_uid, rnd["round_number"], e)
            labels = {lid: False for lid in label_ids}

    row = {
        "episode_uid": episode_uid,
        "round_number": rnd["round_number"],
        "round_outcome": rnd.get("round_outcome", ""),
        "joint_efficiency": rnd.get("joint_efficiency", ""),
    }
    row.update({lid: int(labels[lid]) for lid in label_ids})
    return row, thinking


async def run_all(
    games: list[dict],
    system_prompt: str,
    label_ids: list[str],
    api_format: str,
    api_base: str,
    api_key: str,
    concurrency: int,
    already_done: set[tuple],
    output_path: Path,
    thinking_log_path: Path,
) -> None:
    semaphore = asyncio.Semaphore(concurrency)
    loop = asyncio.get_event_loop()

    # Build task list, skipping already-labeled rounds
    tasks = []
    skipped = 0
    for environment in games:
        gid = environment["episode_uid"]
        for rnd in environment["rounds"]:
            key = (gid, rnd["round_number"])
            if key in already_done:
                skipped += 1
                continue
            tasks.append((gid, rnd))

    log.info("Rounds to label: %d (skipping %d already done)", len(tasks), skipped)
    if not tasks:
        log.info("Nothing to do.")
        return

    total = len(tasks)
    done = 0

    # Write CSV and thinking log incrementally
    with open(output_path, "a", newline="") as csvfile, \
         open(thinking_log_path, "a") as thinkfile:
        writer = csv.DictWriter(
            csvfile,
            fieldnames=["episode_uid", "round_number", "round_outcome", "joint_efficiency"] + label_ids,
        )

        async def do_task(gid, rnd):
            nonlocal done
            row, thinking = await label_round(
                semaphore, loop, None, gid, rnd,
                system_prompt, label_ids, api_format, api_base, api_key,
            )
            writer.writerow(row)
            csvfile.flush()
            if thinking:
                thinkfile.write(json.dumps({
                    "episode_uid": gid,
                    "round_number": rnd["round_number"],
                    "thinking": thinking,
                }) + "\n")
                thinkfile.flush()
            done += 1
            if done % 50 == 0 or done == total:
                log.info("Progress: %d / %d rounds (%.1f%%)", done, total, done / total * 100)

        await asyncio.gather(*(do_task(gid, rnd) for gid, rnd in tasks))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _load_sample_rounds(sample_path: Path) -> list[dict]:
    with open(sample_path) as f:
        sample = json.load(f)
    games: dict[str, dict] = {}
    for item in sample.get("rounds", []):
        gid = item["episode_uid"]
        if gid not in games:
            games[gid] = {
                "episode_uid": gid,
                "model_a": item.get("model_a"),
                "model_b": item.get("model_b"),
                "mode": item.get("mode"),
                "mc_ratio": item.get("mc_ratio"),
                "rounds": [],
            }
        games[gid]["rounds"].append(item["round"])
    return list(games.values())


def _load_judged_games(judgments_csv: Path, extracted_dir: Path) -> list[dict]:
    judged_ids: set[str] = set()
    with open(judgments_csv) as f:
        for row in csv.DictReader(f):
            judged_ids.add(row["episode_uid"])
    log.info("Judged environment set: %d games", len(judged_ids))

    games: list[dict] = []
    for path in sorted(extracted_dir.glob("*.json")):
        gid = path.stem
        if gid not in judged_ids:
            continue
        with open(path) as f:
            games.append(json.load(f))
    return games


def main() -> None:
    parser = argparse.ArgumentParser(description="Label rounds with a taxonomy rubric via Gemini (Vertex AI).")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY_PATH)
    parser.add_argument("--sample", type=Path, default=None,
                        help="Optional calibration_rounds JSON. If set, label only these rounds.")
    parser.add_argument("--extracted-dir", type=Path, default=DEFAULT_EXTRACTED_DIR)
    parser.add_argument("--judgments-csv", type=Path, default=DEFAULT_JUDGMENTS_CSV)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--thinking-log", type=Path, default=DEFAULT_THINKING_LOG)
    args = parser.parse_args()

    # Resolve provider config — vertexai uses ADC, no API key needed
    if PROVIDER not in LLM_PROVIDERS:
        raise SystemExit(f"Unknown provider '{PROVIDER}' in config.LLM_PROVIDERS.")
    provider = LLM_PROVIDERS[PROVIDER]
    api_format: str = provider["api_format"]
    api_base: str = provider["api_base"]
    api_key: str = ""
    log.info("Provider: %s  Model: %s", PROVIDER, MODEL)

    # Load taxonomy
    label_ids, rubric_text = load_taxonomy(args.taxonomy)
    log.info("Loaded %d labels: %s", len(label_ids), label_ids)
    system_prompt = build_system_prompt(rubric_text, label_ids)

    if args.sample:
        games = _load_sample_rounds(args.sample)
        log.info("Loaded calibration sample: %s", args.sample)
    else:
        games = _load_judged_games(args.judgments_csv, args.extracted_dir)

    total_rounds = sum(len(g["rounds"]) for g in games)
    log.info("Games loaded: %d, total rounds: %d", len(games), total_rounds)

    # Resume: load already-labeled (episode_uid, round_number) pairs
    already_done: set[tuple] = set()
    fieldnames = ["episode_uid", "round_number", "round_outcome", "joint_efficiency"] + label_ids
    if args.output.exists():
        with open(args.output) as f:
            for row in csv.DictReader(f):
                already_done.add((row["episode_uid"], int(row["round_number"])))
        log.info("Resuming: %d rounds already labeled", len(already_done))
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=fieldnames).writeheader()
        log.info("Created output: %s", args.output)

    log.info("Thinking log: %s", args.thinking_log)
    asyncio.run(run_all(
        games=games,
        system_prompt=system_prompt,
        label_ids=label_ids,
        api_format=api_format,
        api_base=api_base,
        api_key=api_key,
        concurrency=args.concurrency,
        already_done=already_done,
        output_path=args.output,
        thinking_log_path=args.thinking_log,
    ))

    # Final count
    with open(args.output) as f:
        n = sum(1 for _ in csv.DictReader(f))
    log.info("Done. %d rounds labeled → %s", n, args.output)


if __name__ == "__main__":
    main()
