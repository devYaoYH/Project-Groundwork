"""Developer catalogue discovery and private counts do not create catalogue SQL."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from a2a_engine.registry import get_environment_spec
from cloud_platform.catalogue import LocalCatalogue, LocalCatalogueStore, installed_manifest
from cloud_platform.tests.test_api import platform, sign_in, create_experiment
from local_stack.control_plane import ControlPlane

WORKSPACE = Path(__file__).resolve().parents[2]


def test_local_catalogue_routes_do_not_construct_a_compiler(platform, monkeypatch):
    app, control, _ = platform
    alice, bob = sign_in(app, "Alice"), sign_in(app, "Bob")
    experiment = create_experiment(alice)

    def unexpected_compiler(*args, **kwargs):
        raise AssertionError("catalogue reads must not construct a temporary SQLite control plane")

    monkeypatch.setattr(control, "compiler", unexpected_compiler)
    monkeypatch.setattr(ControlPlane, "__init__", unexpected_compiler)
    for _ in range(2):
        assert alice.get("/api/releases").status_code == 200
        environments = alice.get("/api/environments").json()["environments"]
        assert next(row for row in environments if row["environment_id"] == "buyer_seller")["experiment_count"] == 1
        assert all(row["experiment_count"] == 0 for row in bob.get("/api/environments").json()["environments"])
        for suffix in ("", "/items", "/workflow"):
            result = alice.get("/api/environments/buyer_seller" + suffix)
            assert result.status_code == 200, result.text
        assert alice.get("/api/agent-pool").status_code == 200
        validated = alice.post("/api/designs/validate", json={"release_id": experiment["release_id"],
            "design_text": experiment["design_text"]})
        assert validated.status_code == 200 and validated.json()["valid"]
    snapshots = list((control.repo.data_dir / "catalogues").glob("*.json"))
    assert len(snapshots) == 1
    document = json.loads(snapshots[0].read_bytes())
    assert "owner_id" not in document and "experiment_count" not in snapshots[0].read_text()
    assert control.local_catalogue is control.local_catalogue


def test_published_catalogue_matches_existing_ui_contracts(tmp_path):
    catalogue = LocalCatalogue(WORKSPACE, tmp_path / "catalogue")
    original = ControlPlane(tmp_path / "original.sqlite", workspace=WORKSPACE)
    assert catalogue.environment_summaries() == original.environment_summaries()
    for environment in catalogue.environment_summaries():
        identifier = environment["environment_id"]
        actual, expected = catalogue.environment_detail(identifier), original.environment_detail(identifier)
        assert yaml.safe_load(actual.pop("declaration_yaml")) == yaml.safe_load(expected.pop("declaration_yaml"))
        assert actual == expected
        assert catalogue.environment_items(identifier, limit=1) == original.environment_items(identifier, limit=1)
    assert catalogue.available_experiments() == original.available_experiments()
    assert catalogue.agent_pool() == original.agent_pool()
    with pytest.raises(RuntimeError, match="does not provide a SQLite"):
        catalogue._session()


def test_local_publication_is_idempotent_and_retains_previous_catalogues(tmp_path):
    manifest = installed_manifest(WORKSPACE)
    store = LocalCatalogueStore(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        documents = list(pool.map(lambda _: store.publish(manifest), range(8)))
    assert all(document == documents[0] for document in documents)
    assert len(list(store.root.glob("*.json"))) == 1
    updated = json.loads(json.dumps(manifest))
    updated["environments"][0]["declaration"]["blurb"] += " Changed for developer staging."
    second = store.publish(updated)
    assert second["catalogue_sha256"] != documents[0]["catalogue_sha256"]
    assert len(list(store.root.glob("*.json"))) == 2
    assert store.publish(manifest) == documents[0]
    for path in store.root.iterdir():
        assert not path.name.startswith(".catalogue-")
    old_path = store.root / (documents[0]["catalogue_sha256"] + ".json")
    corrupt = json.loads(old_path.read_bytes())
    corrupt["manifest"]["schema_version"] = 100
    old_path.write_text(json.dumps(corrupt))
    with pytest.raises(ValueError, match="checksum mismatch"):
        store.publish(manifest)


def test_newly_installed_game_is_discovered_without_an_infrastructure_list(tmp_path, monkeypatch):
    from a2a_engine import registry
    from scripts import export_environment_ids

    identifiers = export_environment_ids.environment_ids()
    template = get_environment_spec("buyer_seller")
    declaration = template.declaration.model_copy(update={"id": "developer_game", "environment_id": "developer_game",
        "version": "dev", "blurb": "New developer staging environment"})
    spec = template._replace(declaration=declaration)
    monkeypatch.setitem(registry._REGISTRY, "developer_game", spec)
    monkeypatch.setattr(export_environment_ids, "installed_environments", lambda: identifiers + ["developer_game"])
    manifest = installed_manifest(WORKSPACE)
    file = tmp_path / "catalogue.json"
    file.write_text(json.dumps(manifest))
    catalogue = LocalCatalogue(WORKSPACE, tmp_path / "data", file)
    assert next(row for row in catalogue.environment_summaries() if row["environment_id"] == "developer_game")["release_id"] == "developer_game"
    assert catalogue.environment_detail("developer_game")["release"]["version"] == "dev"
    # A subsequent API process reads the same published document after restart.
    assert LocalCatalogue(WORKSPACE, tmp_path / "data", file).document == catalogue.document
