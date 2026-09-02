#!/usr/bin/env python3
"""Convert legacy negotiation Firestore exports into canonical trace JSON.

Usage:
    python scripts/backfill_negotiation_firestore.py legacy.jsonl output-dir

The input is a JSON object, a JSON list, or JSON Lines export containing the
old ``{game_config, result}`` document envelope. The script performs no network
calls; use your cloud provider's export tooling outside this repository, then
review and upload the canonical output through the configured sink.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from a2a_engine.storage.firestore import FirestoreTraceStore


def _documents(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        if not isinstance(data, list):
            raise ValueError("JSON array input must contain objects")
        return data
    if text.startswith("{"):
        data = json.loads(text)
        return data.get("documents", []) if isinstance(data.get("documents"), list) else [data]
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Firestore export as JSON, JSON array, or JSONL")
    parser.add_argument("output_dir", type=Path, help="Destination for canonical GameTraceBase JSON files")
    args = parser.parse_args()

    store = FirestoreTraceStore(results_dir=args.output_dir)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for index, document in enumerate(_documents(args.input), start=1):
        trace = store.from_document(document)
        if not trace.game_id:
            raise ValueError(f"document {index} has no game_id")
        (args.output_dir / f"{trace.game_id}.json").write_text(
            trace.model_dump_json(indent=2) + "\n", encoding="utf-8"
        )
        written += 1
    print(f"converted={written} output_dir={args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
