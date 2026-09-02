"""Game-level taxonomy labeling with one LLM call per game.

This is the v3 calibration rater path: each API call receives the full game
transcript, including all prior rounds, and returns labels for every round in
that game. Calls are parallelized across games, then flattened to one CSV row
per (game_id, round_number).

Prepared for repeated stability runs, but this module does not orchestrate the
five repeats itself. Run it with distinct output paths for each repeat.

Usage:
    uv run python -m judge.run_taxonomy_game_labeling \
      --taxonomy judge/output/taxonomy_v3.json \
      --sample judge/output/exploratory_10games_v3.json \
      --output judge/output/taxonomy_labels_gemini_v3_exploratory_repeat01.csv
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

from negotiation_game.backend.agents.api import call_llm_oneshot_with_thinking
from negotiation_game.backend.defaults import (
    API_BACKOFF_BASE,
    API_BACKOFF_MAX,
    API_MAX_RETRIES,
    LLM_PROVIDERS,
    REPO_ROOT,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-24s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("judge.taxonomy_game_labeling")

MODEL = "publishers/google/models/gemini-3-flash-preview"
PROVIDER = "gemini_vertexai"
TEMPERATURE = 0.7
DEFAULT_CONCURRENCY = 10
DEFAULT_MAX_TOKENS = 32768
DEFAULT_THINKING_BUDGET = 0

DEFAULT_TAXONOMY = REPO_ROOT / "judge" / "output" / "taxonomy_v3.json"
DEFAULT_SAMPLE = REPO_ROOT / "judge" / "output" / "exploratory_10games_v3.json"
DEFAULT_OUTPUT = REPO_ROOT / "judge" / "output" / "taxonomy_labels_gemini_v3_exploratory.csv"
DEFAULT_THINKING_LOG = REPO_ROOT / "judge" / "output" / "taxonomy_thinking_gemini_v3_exploratory.jsonl"
FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def load_taxonomy(path: Path) -> tuple[list[str], list[str], str]:
    """Return core label IDs, auxiliary tag IDs, and rubric text for the prompt."""
    with open(path) as f:
        taxonomy = json.load(f)

    label_ids: list[str] = []
    auxiliary_ids: list[str] = []
    sections: list[str] = []
    for bucket_name, polarity in [
        ("negative_patterns", "negative"),
        ("positive_patterns", "positive"),
    ]:
        for pattern in taxonomy.get(bucket_name, []):
            label_ids.append(pattern["id"])
            inclusion = "\n".join(f"  - {c}" for c in pattern.get("inclusion_criteria", []))
            exclusion = "\n".join(f"  - {c}" for c in pattern.get("exclusion_criteria", []))
            grounding = pattern.get("grounding_mechanism", "")
            sections.append(
                f"### {pattern['id']} ({pattern['label']}) [{polarity}]\n"
                f"Definition: {pattern.get('definition') or pattern.get('description', '')}\n"
                f"Grounding mechanism: {grounding}\n"
                f"Inclusion criteria (ALL must hold):\n{inclusion}\n"
                f"Exclusion criteria:\n{exclusion}\n"
                f"Example: {pattern.get('example', '(none provided)')}"
            )

    auxiliary_sections: list[str] = []
    for tag in taxonomy.get("auxiliary_tags", []):
        auxiliary_ids.append(tag["id"])
        inclusion = "\n".join(f"  - {c}" for c in tag.get("inclusion_criteria", []))
        exclusion = "\n".join(f"  - {c}" for c in tag.get("exclusion_criteria", []))
        auxiliary_sections.append(
            f"### {tag['id']} ({tag['label']}) [auxiliary]\n"
            f"Definition: {tag.get('definition') or tag.get('description', '')}\n"
            f"Inclusion criteria (ALL must hold):\n{inclusion}\n"
            f"Exclusion criteria:\n{exclusion}\n"
            f"Example: {tag.get('example', '(none provided)')}"
        )

    rubric_text = "## Core Labels\n\n" + "\n\n".join(sections)
    if auxiliary_sections:
        rubric_text += "\n\n## Auxiliary Tags\n\n" + "\n\n".join(auxiliary_sections)

    return label_ids, auxiliary_ids, rubric_text


def build_system_prompt(rubric_text: str, label_ids: list[str], auxiliary_ids: list[str]) -> str:
    label_template = ",\n        ".join(f'"{label}": false' for label in label_ids)
    auxiliary_template = ",\n        ".join(
        f'"{tag}": {{"present": false, "agents": []}}'
        for tag in auxiliary_ids
    )
    return f"""You are an expert annotator for multi-agent negotiation research.

You will receive one complete multi-round negotiation game. Label each round
using the rubric below. Use the full game context when deciding round labels:
prior negotiation transcripts, repeated patterns, repairs, regression, and
precedent all matter. However, each output row should label only the mechanism
present in that specific round.

Apply AND-logic for inclusion criteria. Be strict. A round may receive multiple
labels, but do not label generic helpful tactics unless they instantiate one of
the grounding mechanisms in the rubric.

Core labels are round-level only. They do not have agent attribution.

Auxiliary tags are separate from core labels. For each auxiliary tag, mark:
- "present": whether the behavior appears anywhere in the current round.
- "agents": which agents exhibit it, using only "agent_a" and/or "agent_b".
  Valid values are [], ["agent_a"], ["agent_b"], or ["agent_a", "agent_b"].
- If present is false, agents must be [].
- If present is true, agents must contain at least one agent.
- Auxiliary tags must be based on public speech only. Do not assign auxiliary
  tags based solely on private thinking.

Use the full game context, but only tag behavior exhibited in the current round.
Auxiliary tags may co-occur with any core labels and with each other.

## Rubric

{rubric_text}

## Response format

Respond with ONLY a JSON object:
{{
  "rounds": [
    {{
      "round_number": 1,
      "core_labels": {{
        {label_template}
      }},
      "auxiliary_tags": {{
        {auxiliary_template}
      }}
    }}
  ]
}}

Include exactly one object for every round in the game. Use true/false values.
If a round has no applicable labels or tags, still include that round with all
core labels set to false and all auxiliary tags set to {{"present": false,
"agents": []}}. Never omit a round.
No explanations, no markdown fences.
"""


def load_sample_games(sample_path: Path) -> list[dict]:
    """Load calibration/exploratory sample and group round payloads by game."""
    with open(sample_path) as f:
        sample = json.load(f)

    games_by_id: dict[str, dict] = {}
    for item in sample.get("rounds", []):
        game_id = item["game_id"]
        if game_id not in games_by_id:
            games_by_id[game_id] = {
                "game_id": game_id,
                "model_a": item.get("model_a"),
                "model_b": item.get("model_b"),
                "mode": item.get("mode"),
                "shifting_agent": item.get("shifting_agent"),
                "mc_ratio": item.get("mc_ratio"),
                "experiment_label": item.get("experiment_label"),
                "experiment_run_id": item.get("experiment_run_id"),
                "rounds": [],
            }
        games_by_id[game_id]["rounds"].append(item["round"])

    games = list(games_by_id.values())
    for game in games:
        game["rounds"].sort(key=lambda r: int(r["round_number"]))
    return games


def allocation_text(allocation: dict | None) -> str:
    if not allocation:
        return "none"
    return ", ".join(f"{resource}={amount}" for resource, amount in sorted(allocation.items()))


def format_turn(turn: dict) -> list[str]:
    speaker = turn.get("speaker", "?")
    turn_number = turn.get("turn", "?")
    label = f"[{speaker} turn {turn_number}]"
    if turn.get("is_decision"):
        label += " (decision)"

    lines: list[str] = []
    thinking = turn.get("thinking")
    speech = turn.get("speech")
    if thinking:
        lines.append(f"{label} THINKING: {thinking}")
    if speech:
        lines.append(f"{label} SPEECH: {speech}")
    return lines


def format_round_for_prompt(round_data: dict) -> str:
    lines = [
        f"## Round {round_data['round_number']}",
        f"Outcome: {round_data.get('round_outcome', '?')}",
        f"Joint efficiency: {round_data.get('joint_efficiency', '?')}",
        f"Reward agent_a: {round_data.get('reward_a', '?')}",
        f"Reward agent_b: {round_data.get('reward_b', '?')}",
        "",
        "Transcript:",
    ]

    agent_turns = [
        turn for turn in round_data.get("cheap_talk", [])
        if turn.get("speaker") in ("agent_a", "agent_b")
    ]
    if agent_turns:
        for turn in agent_turns:
            lines.extend(format_turn(turn))
    else:
        lines.append("(No agent cheap-talk transcript for this round.)")

    lines.extend([
        "",
        "Final submissions:",
        f"agent_a: {allocation_text(round_data.get('allocation_a'))}",
        f"agent_b: {allocation_text(round_data.get('allocation_b'))}",
    ])
    return "\n".join(lines)


def format_game_prompt(game: dict) -> str:
    lines = [
        f"# Game {game['game_id']}",
        f"model_a: {game.get('model_a')}",
        f"model_b: {game.get('model_b')}",
        f"mode: {game.get('mode')}",
        f"shifting_agent: {game.get('shifting_agent')}",
        f"mc_ratio: {game.get('mc_ratio')}",
        f"experiment_label: {game.get('experiment_label')}",
        "",
        "The rounds below are chronological. Use earlier rounds as context for later labels.",
        "",
    ]
    for round_data in game["rounds"]:
        lines.append(format_round_for_prompt(round_data))
        lines.append("")
    return "\n".join(lines)


def parse_label_response(
    text: str,
    label_ids: list[str],
    auxiliary_ids: list[str],
    expected_rounds: set[int],
) -> dict[int, dict]:
    cleaned = text.strip()
    fenced = FENCE_RE.search(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        data = repair_json(cleaned, return_objects=True)

    if not isinstance(data, dict):
        raise ValueError("Response was not a JSON object.")

    rounds = data.get("rounds")
    if not isinstance(rounds, list):
        raise ValueError("Response JSON must contain a 'rounds' list.")

    parsed: dict[int, dict] = {}
    for row in rounds:
        if not isinstance(row, dict):
            continue
        round_number = int(row.get("round_number"))
        if round_number not in expected_rounds:
            continue

        core_source = row.get("core_labels")
        if not isinstance(core_source, dict):
            core_source = row

        aux_source = row.get("auxiliary_tags")
        if not isinstance(aux_source, dict):
            aux_source = {}

        parsed[round_number] = {
            "core_labels": {
                label: coerce_bool(core_source.get(label, False))
                for label in label_ids
            },
            "auxiliary_tags": {
                tag: parse_auxiliary_tag(aux_source.get(tag), row.get(tag))
                for tag in auxiliary_ids
            },
        }

    missing = expected_rounds - set(parsed)
    if missing:
        raise ValueError(f"Missing labels for rounds: {sorted(missing)}")
    return parsed


def parse_auxiliary_tag(value, fallback_value=None) -> dict:
    """Parse {"present": bool, "agents": [...]} with a lenient flat fallback."""
    if isinstance(value, dict):
        present = coerce_bool(value.get("present", False))
        raw_agents = value.get("agents", [])
    else:
        present = coerce_bool(fallback_value if fallback_value is not None else value)
        raw_agents = []

    if not isinstance(raw_agents, list):
        raw_agents = [raw_agents]
    agents = [
        agent for agent in raw_agents
        if agent in ("agent_a", "agent_b")
    ]
    if not present:
        agents = []
    return {"present": present, "agents": sorted(set(agents))}


def coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1"}
    return False


def call_game_with_retry(
    game: dict,
    system_prompt: str,
    label_ids: list[str],
    auxiliary_ids: list[str],
    model: str,
    api_format: str,
    api_base: str,
    api_key: str,
    max_tokens: int,
    thinking_budget: int | None,
) -> tuple[dict[int, dict], str | None]:
    expected_rounds = {int(round_data["round_number"]) for round_data in game["rounds"]}
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": format_game_prompt(game)},
    ]

    last_error: Exception | None = None
    for attempt in range(API_MAX_RETRIES):
        try:
            text, thinking = call_llm_oneshot_with_thinking(
                api_format=api_format,
                api_base=api_base,
                api_key=api_key,
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=TEMPERATURE,
                thinking_budget=thinking_budget if thinking_budget and thinking_budget > 0 else None,
            )
            labels = parse_label_response(text, label_ids, auxiliary_ids, expected_rounds)
            return labels, thinking
        except urllib.error.HTTPError as err:
            last_error = err
            if err.code not in (429, 500, 502, 503, 529):
                raise
        except (OSError, TimeoutError, TypeError, ValueError) as err:
            last_error = err

        wait = min(API_BACKOFF_BASE * (2 ** attempt), API_BACKOFF_MAX)
        log.warning(
            "%s failed on attempt %d/%d: %s; sleeping %.1fs",
            game["game_id"],
            attempt + 1,
            API_MAX_RETRIES,
            last_error,
            wait,
        )
        time.sleep(wait)

    raise RuntimeError(f"Failed {game['game_id']} after {API_MAX_RETRIES} attempts: {last_error}")


def auxiliary_columns(auxiliary_ids: list[str]) -> list[str]:
    columns: list[str] = []
    for tag in auxiliary_ids:
        columns.extend([tag, f"{tag}_agent_a", f"{tag}_agent_b"])
    return columns


def rows_for_game(
    game: dict,
    labels_by_round: dict[int, dict],
    label_ids: list[str],
    auxiliary_ids: list[str],
) -> list[dict]:
    rows: list[dict] = []
    for round_data in game["rounds"]:
        round_number = int(round_data["round_number"])
        label_values = labels_by_round[round_number]["core_labels"]
        aux_values = labels_by_round[round_number]["auxiliary_tags"]
        row = {
            "game_id": game["game_id"],
            "round_number": round_number,
            "round_outcome": round_data.get("round_outcome", ""),
            "joint_efficiency": round_data.get("joint_efficiency", ""),
        }
        row.update({label: int(label_values[label]) for label in label_ids})
        for tag in auxiliary_ids:
            tag_value = aux_values[tag]
            agents = set(tag_value["agents"])
            row[tag] = int(tag_value["present"])
            row[f"{tag}_agent_a"] = int("agent_a" in agents)
            row[f"{tag}_agent_b"] = int("agent_b" in agents)
        rows.append(row)
    return rows


async def label_one_game(
    semaphore: asyncio.Semaphore,
    loop: asyncio.AbstractEventLoop,
    game: dict,
    system_prompt: str,
    label_ids: list[str],
    auxiliary_ids: list[str],
    model: str,
    api_format: str,
    api_base: str,
    api_key: str,
    max_tokens: int,
    thinking_budget: int,
) -> tuple[list[dict], dict | None]:
    async with semaphore:
        labels, thinking = await loop.run_in_executor(
            None,
            lambda: call_game_with_retry(
                game=game,
                system_prompt=system_prompt,
                label_ids=label_ids,
                auxiliary_ids=auxiliary_ids,
                model=model,
                api_format=api_format,
                api_base=api_base,
                api_key=api_key,
                max_tokens=max_tokens,
                thinking_budget=thinking_budget,
            ),
        )
    thinking_row = None
    if thinking:
        thinking_row = {"game_id": game["game_id"], "thinking": thinking}
    return rows_for_game(game, labels, label_ids, auxiliary_ids), thinking_row


async def run_all_games(
    games: list[dict],
    system_prompt: str,
    label_ids: list[str],
    auxiliary_ids: list[str],
    model: str,
    api_format: str,
    api_base: str,
    api_key: str,
    output_path: Path,
    thinking_log_path: Path,
    concurrency: int,
    max_tokens: int,
    thinking_budget: int,
) -> None:
    semaphore = asyncio.Semaphore(concurrency)
    loop = asyncio.get_event_loop()
    tasks = [
        label_one_game(
            semaphore=semaphore,
            loop=loop,
            game=game,
            system_prompt=system_prompt,
            label_ids=label_ids,
            auxiliary_ids=auxiliary_ids,
            model=model,
            api_format=api_format,
            api_base=api_base,
            api_key=api_key,
            max_tokens=max_tokens,
            thinking_budget=thinking_budget,
        )
        for game in games
    ]

    log.info("Games to label: %d (concurrency=%d)", len(tasks), concurrency)
    results = await asyncio.gather(*tasks)

    rows: list[dict] = []
    thinking_rows: list[dict] = []
    for game_rows, thinking_row in results:
        rows.extend(game_rows)
        if thinking_row:
            thinking_rows.append(thinking_row)

    rows.sort(key=lambda r: (r["game_id"], int(r["round_number"])))
    fieldnames = [
        "game_id",
        "round_number",
        "round_outcome",
        "joint_efficiency",
        *label_ids,
        *auxiliary_columns(auxiliary_ids),
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    thinking_log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(thinking_log_path, "w") as f:
        for row in thinking_rows:
            f.write(json.dumps(row) + "\n")

    log.info("Wrote %d round rows -> %s", len(rows), output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Label taxonomy categories with one LLM call per game.")
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY)
    parser.add_argument("--sample", type=Path, default=DEFAULT_SAMPLE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--thinking-log", type=Path, default=DEFAULT_THINKING_LOG)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--thinking-budget", type=int, default=DEFAULT_THINKING_BUDGET)
    parser.add_argument("--model", default=MODEL)
    args = parser.parse_args()

    provider = LLM_PROVIDERS[PROVIDER]
    label_ids, auxiliary_ids, rubric_text = load_taxonomy(args.taxonomy)
    games = load_sample_games(args.sample)
    system_prompt = build_system_prompt(rubric_text, label_ids, auxiliary_ids)

    log.info("Provider: %s  Model: %s", PROVIDER, args.model)
    log.info("Loaded %d core labels: %s", len(label_ids), label_ids)
    log.info("Loaded %d auxiliary tags: %s", len(auxiliary_ids), auxiliary_ids)
    log.info("Loaded %d games / %d rounds from %s", len(games), sum(len(g["rounds"]) for g in games), args.sample)
    log.info("Thinking budget: %d; max output tokens: %d", args.thinking_budget, args.max_tokens)

    asyncio.run(run_all_games(
        games=games,
        system_prompt=system_prompt,
        label_ids=label_ids,
        auxiliary_ids=auxiliary_ids,
        model=args.model,
        api_format=provider["api_format"],
        api_base=provider["api_base"],
        api_key="",
        output_path=args.output,
        thinking_log_path=args.thinking_log,
        concurrency=args.concurrency,
        max_tokens=args.max_tokens,
        thinking_budget=args.thinking_budget,
    ))


if __name__ == "__main__":
    main()
