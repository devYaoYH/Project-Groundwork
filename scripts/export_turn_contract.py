#!/usr/bin/env python3
"""Export/check a version-selected deterministic public turn schema bundle."""

import argparse
import json
from pathlib import Path

from a2a_engine.remote.contract import contract_schemas


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--protocol-version", choices=["a2a-turns/1", "a2a-turns/2"], default="a2a-turns/1")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output is None:
        args.output = Path(__file__).resolve().parents[1] / f"docs/a2a-turns-{args.protocol_version[-1]}.schema.json"
    expected = json.dumps(contract_schemas(args.protocol_version), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not args.output.exists() or args.output.read_text(encoding="utf-8") != expected:
            print(f"{args.protocol_version} schema is missing or stale; run scripts/export_turn_contract.py with version selection")
            return 1
    else:
        args.output.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
