"""Install all trusted workspace packages and entry-point environment packages.

Called by the image build, so adding a game does not require changing Dockerfiles.
Only repository-controlled packages that declare the environment entry point ship.
"""

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


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    arguments = [sys.executable, "-m", "pip", "install", "--no-cache-dir"]
    for package in packages(root):
        arguments.extend(["-e", str(package)])
    subprocess.run(arguments, check=True)
