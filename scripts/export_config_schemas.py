#!/usr/bin/env python3
"""Export the versioned Pydantic configuration contracts as JSON Schema.

The Python models remain the single source of truth.  This helper gives YAML
editors, CI jobs, and downstream tooling a checked-in-or-generated machine
readable surface without maintaining a second handwritten schema.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from a2a_engine.environment import ReleaseDeclaration, ExperimentConfig


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export ReleaseDeclaration and ExperimentConfig JSON Schemas."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("schemas"),
        help="directory for release-config.v1.json and experiment-config.v1.json",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for filename, model in (
        ("release-config.v1.json", ReleaseDeclaration),
        ("experiment-config.v1.json", ExperimentConfig),
    ):
        path = args.output_dir / filename
        path.write_text(
            json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
