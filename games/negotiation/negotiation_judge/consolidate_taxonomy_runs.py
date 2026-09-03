"""Combine completed taxonomy run CSVs into one downstream analysis CSV."""

import argparse
import csv
from pathlib import Path

from negotiation_game.backend.defaults import REPO_ROOT

DEFAULT_OUTPUT = REPO_ROOT / "judge" / "output" / "taxonomy_runs" / "taxonomy_v3_combined.csv"


def read_rows(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def main() -> None:
    parser = argparse.ArgumentParser(description="Combine taxonomy run CSV files.")
    parser.add_argument("--input", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    all_fieldnames: list[str] = []
    all_rows: list[dict] = []
    for path in args.input:
        fieldnames, rows = read_rows(path)
        for fieldname in fieldnames:
            if fieldname not in all_fieldnames:
                all_fieldnames.append(fieldname)
        all_rows.extend(rows)

    all_rows.sort(key=lambda r: (
        r.get("experiment_label", ""),
        r.get("label_model", ""),
        r.get("episode_uid", ""),
        int(r.get("round_number") or 0),
    ))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=all_fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"Wrote {len(all_rows)} rows -> {args.output}")


if __name__ == "__main__":
    main()
