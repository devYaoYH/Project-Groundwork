#!/usr/bin/env python3
"""Export/check the deterministic a2a-turns/1 public schema bundle."""

import argparse
import json
from pathlib import Path

from a2a_engine.remote.contract import contract_schemas


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "docs/a2a-turns-1.schema.json")
    args = parser.parse_args()
    expected = json.dumps(contract_schemas(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != expected:
            print("a2a-turns/1 schema is missing or stale; run scripts/export_turn_contract.py")
            return 1
    else:
        args.output.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
