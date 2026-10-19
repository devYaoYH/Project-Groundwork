"""Workspace dependency export is shared by installation and image builds."""

from scripts.install_workspace import external_requirements
from tests.workspace.test_discovery import workspace


def test_exported_dependencies_preserve_constraints_and_exclude_local_distributions(tmp_path):
    package = workspace(tmp_path)
    core = tmp_path / "a2a-engine/pyproject.toml"
    core.write_text(
        core.read_text().replace(
            "[project]", '[project]\ndependencies = ["httpx>=0.28", "new_game>=0.1"]'
        )
    )
    manifest = package / "pyproject.toml"
    manifest.write_text(
        manifest.read_text().replace(
            "[project]",
            """[project]\ndependencies = ["a2a_engine", "httpx<0.29", "numpy>=2; python_version >= '3.12'"]""",
        )
    )
    assert external_requirements(tmp_path) == [
        "httpx<0.29",
        "httpx>=0.28",
        "numpy>=2; python_version >= '3.12'",
    ]
