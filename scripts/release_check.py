#!/usr/bin/env python3
"""Fail on release hazards that are easy to reintroduce during migration."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
TEXT_SUFFIXES = {".md", ".yaml", ".yml", ".py", ".json", ".toml", ".html"}
EXCLUDED_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache", "data", "tasks"}


def files() -> list[Path]:
    return [
        path for path in ROOT.rglob("*")
        if path.is_file() and path.suffix in TEXT_SUFFIXES and not any(part in EXCLUDED_PARTS for part in path.parts)
    ]


def scan(pattern: str, message: str, paths: list[Path]) -> list[str]:
    regex = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    failures: list[str] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for match in regex.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            failures.append(f"{path.relative_to(ROOT)}:{line}: {message}")
    return failures


def main() -> int:
    all_files = files()
    public_content = [p for p in all_files if p != Path(__file__).resolve()]
    configurations = [p for p in public_content if p.suffix in {".yaml", ".yml"}]
    failures: list[str] = []
    failures += scan(r"arn:aws:iam::\d+", "committed AWS account ARN", public_content)
    failures += scan(r"(?:^|\s)(?:gcp_project:\s*)(?!\$\{)[A-Za-z0-9][A-Za-z0-9-]+", "literal GCP project id", configurations)
    failures += scan(r"vertex_adc_file:\s*(?:/|~|\\\\)", "absolute ADC credential path", public_content)
    failures += scan(r"/(?:Users|home)/[^\s'\"]*(?:adc|credential)", "personal credential path", public_content)
    failures += scan(r"(?:hs-soil-gemini|hs-social-interaction-lab|calbench-traces|calbench-openskill-ratings)", "account-specific cloud identifier", public_content)
    failures += scan(r"scripts/cloud/|s3_calendar_traces\.sh|\baws-t3\b", "reference to unshipped cloud script or host", public_content)
    failures += scan(r"(?:from|import)\s+backend(?:\.|\s)", "stale pre-vendoring backend import", [p for p in all_files if p.suffix == ".py"])

    workflow = ROOT / ".github" / "workflows" / "ci.yml"
    if not workflow.exists():
        failures.append(".github/workflows/ci.yml: required CI workflow is missing")

    if failures:
        print("release check failed:", *failures, sep="\n  ")
        return 1
    print("release check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
