"""Install trusted workspace packages, or export their external dependencies."""

import argparse
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path


def packages(root: Path) -> list[Path]:
    core = [root / name for name in ("a2a-engine", "a2a-agent", "a2a-judge", "expt-runner")]
    games = []
    for manifest in sorted((root / "games").glob("*/pyproject.toml")):
        metadata = tomllib.loads(manifest.read_text())
        if metadata.get("project", {}).get("entry-points", {}).get("a2a_engine.environments"):
            games.append(manifest.parent)
    if not games:
        raise RuntimeError("No environment packages were discovered")
    return core + games


def external_requirements(root: Path) -> list[str]:
    """Keep every constraint while resolving local distributions from source."""
    projects = [
        tomllib.loads((path / "pyproject.toml").read_text())["project"] for path in packages(root)
    ]

    def canonical(name):
        return re.sub(r"[-_.]+", "-", name).lower()

    names = {canonical(project["name"]) for project in projects}
    requirements = set()
    for project in projects:
        for requirement in project.get("dependencies", []):
            match = re.match(r"[A-Za-z0-9_.-]+", requirement)
            if match is None:
                raise ValueError(f"Invalid package requirement: {requirement}")
            if canonical(match[0]) not in names:
                requirements.add(requirement)
    return sorted(requirements)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements-output", type=Path)
    parser.add_argument("--installer", choices=("auto", "pip", "uv"), default="auto")
    parser.add_argument("--no-deps", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if args.requirements_output:
        args.requirements_output.write_text("\n".join(external_requirements(root)) + "\n")
        return
    use_uv = args.installer == "uv" or (args.installer == "auto" and shutil.which("uv"))
    if use_uv:
        arguments = ["uv", "pip", "install", "--python", sys.executable]
    else:
        arguments = [sys.executable, "-m", "pip", "install"]
    if args.no_deps:
        arguments.append("--no-deps")
    for package in packages(root):
        arguments.extend(["-e", str(package)])
    subprocess.run(arguments, check=True)


if __name__ == "__main__":
    main()
