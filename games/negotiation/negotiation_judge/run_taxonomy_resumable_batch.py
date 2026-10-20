"""Resumable environment-level taxonomy labeling.

Each API call labels one complete environment and writes one raw JSON artifact. This
keeps concurrent calls from writing to the same file and makes the run safe to
resume. Consolidation flattens successful raw artifacts into one CSV.

Usage:
    uv run python -m judge.run_taxonomy_resumable_cell \
      --sample judge/output/main720_taxonomy_v3.json \
      --model publishers/google/models/gemini-3-flash-preview \
      --temperature 0 \
      --max-parallelism 36 \
      --run-name main720_flash_temp0
"""

import argparse
import asyncio
import csv
import json
import logging
import os
import time
import urllib.error
from pathlib import Path

from a2a_engine.llm import api as llm_api
from negotiation_game.backend.defaults import API_BACKOFF_BASE, API_BACKOFF_MAX, API_MAX_RETRIES, LLM_PROVIDERS, REPO_ROOT
from negotiation_judge.run_taxonomy_game_labeling import (
    auxiliary_columns,
    build_system_prompt,
    format_game_prompt,
    load_sample_games,
    load_taxonomy,
    parse_label_response,
    rows_for_game,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-28s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("judge.taxonomy_resumable")

DEFAULT_TAXONOMY = REPO_ROOT / "judge" / "output" / "taxonomy_v3.json"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "judge" / "output" / "taxonomy_runs"
PROVIDER = "gemini_vertexai"
ANTHROPIC_VERTEX_PROVIDER = "anthropic_vertexai"
DEFAULT_MAX_TOKENS = 32768


def call_vertexai_full(
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    thinking_budget: int | None,
) -> dict:
    return llm_api.call_llm_oneshot_full(
        api_format="vertexai", api_base="", api_key="", model=model,
        messages=messages, max_tokens=max_tokens, temperature=temperature,
        thinking_budget=thinking_budget if thinking_budget and thinking_budget > 0 else None,
    )


def call_anthropic_vertex_full(
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
) -> dict:
    """Preserve the batch job's Google project/region selection through engine."""
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        project_id = LLM_PROVIDERS.get("gemini_vertexai", {}).get("gcp_project")
    if not project_id:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is required for Claude-on-Vertex runs.")
    return llm_api.call_llm_oneshot_full(
        api_format="vertexai_anthropic", api_base="", api_key="", model=model,
        messages=messages, max_tokens=max_tokens, temperature=temperature,
        gcp_project=project_id,
        gcp_location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
    )


def call_model_full(
    model: str,
    messages: list[dict],
    max_tokens: int,
    temperature: float,
    thinking_budget: int | None,
) -> tuple[dict, str]:
    if model.startswith("claude-"):
        return call_anthropic_vertex_full(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        ), ANTHROPIC_VERTEX_PROVIDER
    return call_vertexai_full(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        thinking_budget=thinking_budget,
    ), PROVIDER


def raw_path_for_game(raw_dir: Path, episode_uid: str) -> Path:
    return raw_dir / f"{episode_uid}.json"


def load_successful_raw(path: Path, expected_rounds: set[int]) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path) as f:
            data = json.load(f)
    except json.JSONDecodeError:
        return None
    if data.get("status") != "success":
        return None
    labels = data.get("labels_by_round")
    if not isinstance(labels, dict):
        return None
    if {int(k) for k in labels} != expected_rounds:
        return None
    return data


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + f".tmp.{os.getpid()}.{time.time_ns()}")
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, path)


def label_game_sync(
    environment: dict,
    system_prompt: str,
    label_ids: list[str],
    auxiliary_ids: list[str],
    model: str,
    temperature: float,
    max_tokens: int,
    thinking_budget: int,
    raw_dir: Path,
    force: bool,
) -> str:
    episode_uid = environment["episode_uid"]
    expected_rounds = {int(round_data["round_number"]) for round_data in environment["rounds"]}
    output_path = raw_path_for_game(raw_dir, episode_uid)
    if not force and load_successful_raw(output_path, expected_rounds) is not None:
        return "skipped"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": format_game_prompt(environment)},
    ]

    last_error: Exception | None = None
    last_result: dict | None = None
    for attempt in range(API_MAX_RETRIES):
        try:
            result, provider_name = call_model_full(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                thinking_budget=thinking_budget,
            )
            last_result = result
            labels = parse_label_response(result["text"], label_ids, auxiliary_ids, expected_rounds)
            payload = {
                "status": "success",
                "episode_uid": episode_uid,
                "model": model,
                "provider": provider_name,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "thinking_budget": thinking_budget,
                "duration_s": result["duration_s"],
                "created_at_unix": time.time(),
                "game_metadata": {k: v for k, v in environment.items() if k != "rounds"},
                "expected_rounds": sorted(expected_rounds),
                "labels_by_round": {str(k): v for k, v in labels.items()},
                "response_text": result["text"],
                "thinking": result["thinking"],
                "response_metadata": result["response"],
            }
            atomic_write_json(output_path, payload)
            stale_error_path = output_path.with_suffix(".error.json")
            if stale_error_path.exists():
                stale_error_path.unlink()
            log.info("%s success: %d chars in %.1fs", episode_uid, len(result["text"]), result["duration_s"])
            return "success"
        except urllib.error.HTTPError as err:
            last_error = err
            if err.code not in (429, 500, 502, 503, 529):
                break
        except (OSError, TimeoutError, TypeError, ValueError) as err:
            last_error = err

        wait = min(API_BACKOFF_BASE * (2 ** attempt), API_BACKOFF_MAX)
        log.warning("%s failed attempt %d/%d: %s; sleeping %.1fs", episode_uid, attempt + 1, API_MAX_RETRIES, last_error, wait)
        time.sleep(wait)

    error_payload = {
        "status": "error",
        "episode_uid": episode_uid,
        "model": model,
        "provider": ANTHROPIC_VERTEX_PROVIDER if model.startswith("claude-") else PROVIDER,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "thinking_budget": thinking_budget,
        "created_at_unix": time.time(),
        "game_metadata": {k: v for k, v in environment.items() if k != "rounds"},
        "error": repr(last_error),
        "response_text": last_result.get("text") if last_result else None,
        "thinking": last_result.get("thinking") if last_result else None,
        "duration_s": last_result.get("duration_s") if last_result else None,
        "response_metadata": last_result.get("response") if last_result else None,
    }
    atomic_write_json(output_path.with_suffix(".error.json"), error_payload)
    raise RuntimeError(f"{episode_uid} failed after {API_MAX_RETRIES} attempts: {last_error}")


async def run_cell(
    games: list[dict],
    system_prompt: str,
    label_ids: list[str],
    auxiliary_ids: list[str],
    model: str,
    temperature: float,
    max_tokens: int,
    thinking_budget: int,
    raw_dir: Path,
    max_parallelism: int,
    force: bool,
) -> None:
    semaphore = asyncio.Semaphore(max_parallelism)
    loop = asyncio.get_event_loop()

    async def run_one(environment: dict) -> str:
        async with semaphore:
            return await loop.run_in_executor(
                None,
                lambda: label_game_sync(
                    environment=environment,
                    system_prompt=system_prompt,
                    label_ids=label_ids,
                    auxiliary_ids=auxiliary_ids,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    thinking_budget=thinking_budget,
                    raw_dir=raw_dir,
                    force=force,
                ),
            )

    results = await asyncio.gather(*(run_one(environment) for environment in games), return_exceptions=True)
    errors = [result for result in results if isinstance(result, Exception)]
    statuses = [result for result in results if isinstance(result, str)]
    counts = {status: statuses.count(status) for status in sorted(set(statuses))}
    log.info("Cell complete: %s", counts)
    if errors:
        for err in errors[:10]:
            log.error("Cell item failed: %r", err)
        raise RuntimeError(f"{len(errors)} environment(s) failed; rerun the same command to resume after fixing transient issues.")


def consolidate(
    games: list[dict],
    label_ids: list[str],
    auxiliary_ids: list[str],
    raw_dir: Path,
    output_path: Path,
) -> None:
    rows: list[dict] = []
    missing: list[str] = []
    by_game = {environment["episode_uid"]: environment for environment in games}
    for episode_uid, environment in sorted(by_game.items()):
        raw_path = raw_path_for_game(raw_dir, episode_uid)
        expected_rounds = {int(round_data["round_number"]) for round_data in environment["rounds"]}
        raw = load_successful_raw(raw_path, expected_rounds)
        if raw is None:
            missing.append(episode_uid)
            continue
        labels = {int(k): v for k, v in raw["labels_by_round"].items()}
        for row in rows_for_game(environment, labels, label_ids, auxiliary_ids):
            row.update({
                "label_model": raw.get("model"),
                "label_provider": raw.get("provider"),
                "label_temperature": raw.get("temperature"),
                "api_duration_s": raw.get("duration_s"),
                "experiment_label": environment.get("experiment_label"),
                "episode_id": environment.get("episode_id"),
                "model_a": environment.get("model_a"),
                "model_b": environment.get("model_b"),
                "mode": environment.get("mode"),
                "mc_ratio": environment.get("mc_ratio"),
            })
            rows.append(row)

    if missing:
        raise RuntimeError(f"Cannot consolidate; missing/invalid raw outputs for {len(missing)} games. First: {missing[:10]}")

    fieldnames = [
        "episode_uid",
        "round_number",
        "experiment_label",
        "episode_id",
        "model_a",
        "model_b",
        "mode",
        "mc_ratio",
        "round_outcome",
        "joint_efficiency",
        "label_model",
        "label_provider",
        "label_temperature",
        "api_duration_s",
        *label_ids,
        *auxiliary_columns(auxiliary_ids),
    ]
    rows.sort(key=lambda r: (r["episode_uid"], int(r["round_number"])))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    log.info("Wrote %d rows -> %s", len(rows), output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run resumable taxonomy labels one environment per raw output file.")
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-parallelism", "--concurrency", dest="max_parallelism", type=int, default=36)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--thinking-budget", type=int, default=0)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--consolidate-only", action="store_true")
    args = parser.parse_args()

    provider = LLM_PROVIDERS[PROVIDER]
    if provider["api_format"] != "vertexai":
        raise RuntimeError("This resumable runner currently expects the Vertex AI provider.")

    label_ids, auxiliary_ids, rubric_text = load_taxonomy(args.taxonomy)
    system_prompt = build_system_prompt(rubric_text, label_ids, auxiliary_ids)
    games = load_sample_games(args.sample)
    raw_dir = args.output_root / args.run_name / "raw"
    output_path = args.output_root / args.run_name / f"{args.run_name}.csv"

    log.info("Run: %s", args.run_name)
    log.info("Model: %s temperature=%.3f max_parallelism=%d", args.model, args.temperature, args.max_parallelism)
    log.info("Loaded %d games / %d rounds from %s", len(games), sum(len(g['rounds']) for g in games), args.sample)
    log.info("Raw dir: %s", raw_dir)

    if not args.consolidate_only:
        asyncio.run(run_cell(
            games=games,
            system_prompt=system_prompt,
            label_ids=label_ids,
            auxiliary_ids=auxiliary_ids,
            model=args.model,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            thinking_budget=args.thinking_budget,
            raw_dir=raw_dir,
            max_parallelism=args.max_parallelism,
            force=args.force,
        ))

    consolidate(games, label_ids, auxiliary_ids, raw_dir, output_path)


if __name__ == "__main__":
    main()
