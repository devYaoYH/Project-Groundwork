"""Export Next static route IDs from the same installed registry as the catalog.

Run after installing trusted workspace packages. A broken advertised entry point
fails the image build rather than silently disappearing from the shared catalog.
"""

from __future__ import annotations

import argparse
import json
from importlib.metadata import entry_points
from pathlib import Path

from a2a_engine.registry import ENTRY_POINT_GROUP, get_environment_spec, installed_environments


def environment_ids() -> list[str]:
    installed = set(installed_environments())
    advertised = {entry.name for entry in entry_points(group=ENTRY_POINT_GROUP)}
    missing = sorted(advertised - installed)
    if missing:
        raise RuntimeError(f"Installed environment entry points failed to register: {missing}")
    # ControlPlane.seed_installed_releases offers only installed environments
    # whose spec has a release declaration. Use its environment ID, not the
    # declaration's release ID or Python distribution/directory name.
    identifiers = sorted(
        identifier
        for identifier in installed
        if get_environment_spec(identifier).declaration is not None
    )
    if not identifiers:
        raise RuntimeError("No installed environment has a release declaration")
    for identifier in identifiers:
        if identifier in {".", ".."} or "/" in identifier or "\\" in identifier:
            raise ValueError(f"Environment ID cannot be a static route segment: {identifier!r}")
    return identifiers


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "web/lib/environment-ids.json",
    )
    args = parser.parse_args()
    identifiers = environment_ids()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(identifiers, indent=2) + "\n")
    print(f"Exported {len(identifiers)} installed environment routes to {args.output}")


if __name__ == "__main__":
    main()
