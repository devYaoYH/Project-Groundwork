import json
from pathlib import Path

import pytest
import yaml

from scripts.workspace import browser_fixtures, export_site, smoke_document, workspace_manifest


def workspace(tmp_path):
    for core in ("a2a-engine", "a2a-agent", "a2a-judge", "expt-runner"):
        path = tmp_path / core
        path.mkdir()
        (path / "pyproject.toml").write_text(f'[project]\nname = "{core}"\nversion = "0.1.0"\n')
    package = tmp_path / "games/new-game"
    package.mkdir(parents=True)
    (package / "pyproject.toml").write_text("""[project]
name = "new-game"
version = "0.1.0"
[project.entry-points."a2a_engine.environments"]
new_game = "new_game"
[tool.groundwork]
smoke_config = "smoke.yaml"
browser_fixtures = "fixtures.yaml"
""")
    config = {"environment_id": "new_game", "num_rounds": 2, "agents": [{"type": "scripted"}] * 2}
    (package / "smoke.yaml").write_text(yaml.safe_dump(config))
    (package / "fixtures.yaml").write_text(yaml.safe_dump([{"id": "new-game", "config": config}]))
    (tmp_path / "site").mkdir()
    return package


def test_new_package_joins_inventory_smoke_browser_and_site_without_shared_edits(tmp_path):
    workspace(tmp_path)
    assert workspace_manifest(tmp_path)["packages"][-1]["environments"] == {"new_game": "new_game"}
    assert smoke_document(tmp_path)["cells"][0]["config"]["num_rounds"] == 2
    assert browser_fixtures(tmp_path)[0]["id"] == "new-game"
    export_site(tmp_path)
    assert json.loads((tmp_path / "site/games.json").read_text())["games"][0]["id"] == "new_game"


@pytest.mark.parametrize(
    "case", ["missing", "escape", "wrong_environment", "provider", "duplicate"]
)
def test_bad_package_fixture_declarations_fail(tmp_path, case):
    package = workspace(tmp_path)
    if case == "missing":
        (package / "fixtures.yaml").unlink()
    elif case == "escape":
        manifest = package / "pyproject.toml"
        manifest.write_text(manifest.read_text().replace('"fixtures.yaml"', '"../../outside.yaml"'))
    else:
        rows = yaml.safe_load((package / "fixtures.yaml").read_text())
        if case == "wrong_environment":
            rows[0]["config"]["environment_id"] = "unrelated"
        elif case == "provider":
            rows[0]["config"]["agents"] = [{"type": "llm", "model": "provider"}]
        else:
            rows *= 2
        (package / "fixtures.yaml").write_text(yaml.safe_dump(rows))
    with pytest.raises(ValueError):
        browser_fixtures(tmp_path)


def test_repository_smoke_inventory_matches_source_packages():
    root = Path(__file__).resolve().parents[2]
    expected = {
        identifier
        for package in workspace_manifest(root)["packages"]
        for identifier in package["environments"]
    }
    assert {cell["config"]["environment_id"] for cell in smoke_document(root)["cells"]} == expected
