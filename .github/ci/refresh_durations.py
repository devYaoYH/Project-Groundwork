"""Refresh per-file shard weights from one successful run's pytest JUnit reports.

Accept XML files or directories of downloaded python/runtime timing artifacts.
Unmeasured files retain their previous weights. Validation precedes any write.
"""

import argparse
import json
import math
import subprocess
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEIGHTS = Path(__file__).with_name("ci_durations.json")


def module_paths(root):
    """Match pytest xunit2 classnames, including nested classes and hidden paths."""
    output = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "*.py"],
        cwd=root,
    )
    return {
        Path(path).with_suffix("").as_posix().replace("/", "."): path
        for path in output.decode().split("\0")
        if path and (root / path).is_file()
    }


def report_weights(reports, root, modules):
    totals = defaultdict(float)
    seen = set()
    for report in reports:
        tree = ET.parse(report).getroot()
        suites = list(tree.iter("testsuite"))
        if any(int(suite.get(field, "0")) for suite in suites for field in ("failures", "errors")):
            raise ValueError(f"{report}: failed run; use a complete successful run")
        cases = list(tree.iter("testcase"))
        if not cases:
            raise ValueError(f"{report}: no test cases")
        for case in cases:
            if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
                raise ValueError(
                    f"{report}: incomplete case; use reports without failures or skips"
                )
            classname, name = case.get("classname", ""), case.get("name", "")
            identity = (classname, name)
            if not classname or not name or identity in seen:
                raise ValueError(f"{report}: missing or duplicate testcase identity {identity}")
            seen.add(identity)
            if case.get("file"):
                path = (root / case.get("file")).resolve()
                if not path.is_relative_to(root.resolve()) or not path.is_file():
                    raise ValueError(f"{report}: testcase file is outside this checkout or missing")
                relative = path.relative_to(root.resolve()).as_posix()
            else:
                module = classname
                while module not in modules and "." in module:
                    module = module.rsplit(".", 1)[0]
                if module not in modules:
                    raise ValueError(
                        f"{report}: cannot resolve {classname}; use the matching checkout"
                    )
                relative = modules[module]
            duration = float(case.get("time", "nan"))
            if not math.isfinite(duration) or duration < 0:
                raise ValueError(f"{report}: invalid duration for {identity}")
            totals[relative] += duration
    return {path: round(duration, 3) for path, duration in totals.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "reports", nargs="+", type=Path, help="JUnit XML files or artifact directories"
    )
    parser.add_argument(
        "--output", type=Path, default=WEIGHTS, help="default: colocated weights JSON"
    )
    args = parser.parse_args()
    try:
        reports = set()
        for path in args.reports:
            if not path.exists():
                raise ValueError(f"Report path does not exist: {path}")
            reports.update(
                item.resolve() for item in (path.rglob("*.xml") if path.is_dir() else [path])
            )
        if not reports:
            raise ValueError("No XML reports found")
        measured = report_weights(sorted(reports), ROOT, module_paths(ROOT))
        weights = json.loads(args.output.read_text()) if args.output.exists() else {}
        weights.update(measured)
        args.output.write_text(json.dumps(weights, indent=2, sort_keys=True) + "\n")
    except (OSError, ValueError, ET.ParseError, subprocess.CalledProcessError) as error:
        parser.exit(2, f"{error}\n")
    print(f"Refreshed {len(measured)} file weights from {len(reports)} reports: {args.output}")


if __name__ == "__main__":
    main()
