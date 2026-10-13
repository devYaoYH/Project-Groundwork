"""Package-owned integration discovery for CI, smoke runs, browser fixtures and site builds."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory

# Both direct CLI execution and imports from tests/browser setup use this module.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.install_workspace import packages  # noqa: E402


def workspace_manifest(root=ROOT):
    records = []
    identifiers = set()
    for directory in packages(root):
        metadata = tomllib.loads((directory / "pyproject.toml").read_text())
        project = metadata["project"]
        entries = project.get("entry-points", {})
        environments = entries.get("a2a_engine.environments", {})
        if identifiers.intersection(environments):
            raise ValueError("Duplicate workspace environment ID")
        identifiers.update(environments)
        integration = metadata.get("tool", {}).get("groundwork", {})
        if environments and "smoke_config" not in integration:
            raise ValueError(f"Missing package-owned smoke_config: {directory}")
        records.append(
            {
                "path": str(directory.relative_to(root)),
                "name": project["name"],
                "version": project["version"],
                "environments": environments,
                "seats": entries.get("a2a_agent.seats", {}),
                "integration": integration,
            }
        )
    return {"packages": records}


def package_file(root, package, name):
    directory = (root / package["path"]).resolve()
    path = (directory / package["integration"][name]).resolve()
    if not path.is_relative_to(directory) or not path.is_file():
        raise ValueError(f"Invalid package-owned {name}: {package['name']}")
    return path


def smoke_document(root=ROOT):
    import yaml

    cells = []
    for package in workspace_manifest(root)["packages"]:
        if not package["environments"]:
            continue
        config = yaml.safe_load(package_file(root, package, "smoke_config").read_text())
        if (
            not isinstance(config, dict)
            or config.get("environment_id") not in package["environments"]
        ):
            raise ValueError(f"Smoke configuration does not belong to {package['name']}")
        cells.append({"label": config["environment_id"], "count": 1, "config": config})
    return {
        "name": "all_games_smoke",
        "defaults": {"num_agents": 2},
        "cells": cells,
        "storage": {"backend": "sqlite", "path": "results/all_games_smoke.db"},
    }


def browser_fixtures(root=ROOT):
    import yaml

    fixtures = []
    identifiers = set()
    for package in workspace_manifest(root)["packages"]:
        if "browser_fixtures" not in package["integration"]:
            continue
        rows = yaml.safe_load(package_file(root, package, "browser_fixtures").read_text())
        if not isinstance(rows, list):
            raise ValueError("Browser fixtures must be a list")
        for fixture in rows:
            identifier = fixture["id"]
            if (
                identifier in identifiers
                or not isinstance(identifier, str)
                or not identifier
                or "/" in identifier
                or "\\" in identifier
                or identifier in {".", ".."}
            ):
                raise ValueError("Invalid or duplicate browser fixture ID")
            if fixture["config"].get("environment_id") not in package["environments"]:
                raise ValueError("Browser fixture belongs to another package")
            if not fixture["config"].get("agents"):
                raise ValueError("Browser fixtures require explicit scripted agents")
            if any(
                agent.get("type") not in {"scripted", "heuristic"}
                for agent in fixture["config"].get("agents", [])
            ):
                raise ValueError("Browser fixtures must not configure provider agents")
            identifiers.add(identifier)
            fixtures.append(fixture)
    return fixtures


def seed_browser_fixtures(directory, root=ROOT):
    from a2a_engine.manifest import EpisodeManifest
    from a2a_engine.registry import get_environment_spec
    from a2a_engine.storage.sqlite import SQLiteEpisodeStore

    store = SQLiteEpisodeStore(path=Path(directory) / "a2a.db")
    for fixture in browser_fixtures(root):
        config = {
            **fixture["config"],
            "asset_root": str(Path(directory) / fixture["id"] / "generated"),
        }
        trace = get_environment_spec(config["environment_id"]).cls(config, dry_run=True).run()
        trace.episode_uid = f"workflow-browser-{fixture['id']}"
        manifest = EpisodeManifest.from_run(
            config=trace.config.model_dump(),
            experiment_name="workflow-browser",
            cell_id=fixture["id"],
            episode_idx=0,
            episode_uid=trace.episode_uid,
        )
        store.put_episode(trace, manifest)


def export_site(root=ROOT):
    games = []
    for package in workspace_manifest(root)["packages"]:
        if not package["environments"]:
            continue
        if "site_metadata" in package["integration"]:
            game = json.loads(package_file(root, package, "site_metadata").read_text())
        else:
            game = {
                "name": package["name"],
                "status": "active",
                "blurb": "",
                "viewer_url": "",
                "leaderboard_url": "",
                "paper_url": "",
            }
        # Environment identity is canonical; directory/distribution names may differ.
        for identifier in package["environments"]:
            games.append({**game, "id": identifier, "repo": package["path"]})
    (root / "site/games.json").write_text(json.dumps({"games": games}, indent=2) + "\n")


def run(arguments):
    subprocess.run(arguments, cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["test", "build", "check-wheels", "smoke", "export-site"]
    )
    args, extra = parser.parse_known_args()
    manifest = workspace_manifest()
    if args.command == "test":
        # Recurse from each package root: Calendar has two test-directory layouts.
        run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--import-mode=importlib",
                *[item["path"] for item in manifest["packages"]],
                "local_stack/tests",
                "tests/workspace",
                *extra,
            ]
        )
    elif args.command == "build":
        if extra:
            parser.error("build accepts no extra arguments")
        for package in manifest["packages"]:
            run(["uv", "build", package["path"], "--out-dir", "dist"])
    elif args.command == "check-wheels":
        if extra:
            parser.error("check-wheels accepts no extra arguments")
        command = ["uv", "run", "--isolated", "--no-project"]
        for package in manifest["packages"]:
            stem = package["name"].replace("-", "_")
            wheels = list((ROOT / "dist").glob(f"{stem}-{package['version']}-*.whl"))
            if len(wheels) != 1:
                raise ValueError(f"Expected exactly one built wheel: {package['name']}")
            command.extend(["--with", str(wheels[0])])
        with TemporaryDirectory(prefix="workspace-wheels-") as directory:
            expected = Path(directory) / "manifest.json"
            expected.write_text(json.dumps(manifest))
            run(
                [
                    *command,
                    "python",
                    "scripts/check_runtime_packages.py",
                    "--expected-manifest",
                    str(expected),
                ]
            )
    elif args.command == "smoke":
        import yaml

        with TemporaryDirectory(prefix="workspace-smoke-") as directory:
            path = Path(directory) / "all_games_smoke.yaml"
            document = smoke_document()
            document["storage"]["path"] = str(
                Path(os.environ.get("A2A_TRACE_DB", ROOT / "results/all_games_smoke.db")).resolve()
            )
            path.write_text(yaml.safe_dump(document, sort_keys=False))
            run(
                [
                    sys.executable,
                    "-m",
                    "expt_runner.run_experiment",
                    str(path),
                    "--smoke-test",
                    *extra,
                ]
            )
    else:
        if extra:
            parser.error("export-site accepts no extra arguments")
        export_site()


if __name__ == "__main__":
    main()
