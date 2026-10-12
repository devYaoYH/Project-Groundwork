"""Hosted adapter contracts using the real repository and an in-memory SDK client."""
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import AlreadyExists, PermissionDenied
from google.cloud.firestore_v1._helpers import encode_dict

from cloud_platform import catalogue, publish_catalogue
from cloud_platform.api import create_app
from cloud_platform.auth import Auth
from cloud_platform.catalogue import FirestoreCatalogueStore, installed_manifest
from cloud_platform.control import PlatformControl
from cloud_platform.storage import Repository, ArtifactStore
from cloud_platform.tests.test_api import sign_in, create_experiment
from local_stack.control_plane import ControlPlane

WORKSPACE = Path(__file__).resolve().parents[2]


class FirestoreClient:
    """SDK surface fake; no network, emulator, or cloud credentials required."""
    def __init__(self):
        self.documents = {}
        self.lock = threading.Lock()
        self.reads = []
        self.writes = []

    def collection(self, name):
        client = self

        class Collection:
            def document(self, identifier):
                key = (name, identifier)

                class Document:
                    def get(self):
                        with client.lock:
                            client.reads.append(key)
                            data = copy.deepcopy(client.documents.get(key))
                        return SimpleNamespace(to_dict=lambda: data)

                    def create(self, data):
                        with client.lock:
                            if key in client.documents:
                                raise AlreadyExists("already published")
                            # Exercise actual Firestore field encoding as well.
                            encode_dict(data)
                            client.documents[key] = copy.deepcopy(data)
                            client.writes.append(key)

                    def set(self, data):
                        with client.lock:
                            client.documents[key] = copy.deepcopy(data)
                            client.writes.append(key)

                return Document()

            def where(self, *, filter):
                collection = self
                return SimpleNamespace(stream=lambda: [row for row in collection.stream()
                    if row.to_dict().get(filter.field_path) == filter.value])

            def stream(self):
                with client.lock:
                    rows = [copy.deepcopy(value) for (kind, _), value in client.documents.items() if kind == name]
                return [SimpleNamespace(to_dict=lambda row=row: row) for row in rows]

        return Collection()


def repository(client):
    return Repository(backend="gcp", project="test", client=client)


def test_concurrent_publication_retains_versions_and_verifies_read_back():
    client = FirestoreClient()
    store = FirestoreCatalogueStore(repository(client))
    manifest = installed_manifest(WORKSPACE)
    with ThreadPoolExecutor(max_workers=4) as pool:
        documents = list(pool.map(lambda _: store.publish(manifest), range(8)))
    assert all(document == documents[0] for document in documents)
    assert len(client.writes) == 1
    first = documents[0]
    updated = copy.deepcopy(manifest)
    updated["environments"][0]["declaration"]["blurb"] += " New published game metadata."
    second = store.publish(updated)
    assert first["catalogue_sha256"] != second["catalogue_sha256"]
    assert store.load(first["catalogue_sha256"]) == first
    assert len(client.documents) == 2
    assert {kind for kind, _ in client.documents} == {"groundwork_catalogues"}
    serialized = json.dumps(client.documents[("groundwork_catalogues", first["catalogue_sha256"])])
    assert "owner_id" not in serialized and "experiment_count" not in serialized
    assert isinstance(client.documents[("groundwork_catalogues", first["catalogue_sha256"])]["manifest_json"], str)
    client.documents[("groundwork_catalogues", first["catalogue_sha256"])]["manifest_json"] = json.dumps(updated)
    with pytest.raises(ValueError, match="checksum mismatch"):
        store.publish(manifest)


def test_hosted_routes_reuse_snapshot_without_discovery_sql_or_writes(tmp_path, monkeypatch):
    client = FirestoreClient()
    repo = repository(client)
    published = FirestoreCatalogueStore(repo).publish(installed_manifest(WORKSPACE))
    monkeypatch.setenv("PLATFORM_CATALOGUE_SHA256", published["catalogue_sha256"])
    monkeypatch.setenv("PLATFORM_SOURCE_SHA", "test-worker")
    control = PlatformControl(repo, ArtifactStore(backend="local", data_dir=tmp_path), workspace=WORKSPACE)
    app = create_app(control=control, auth=Auth(secret="test-private-session-key-at-least-32-bytes", dev_auth=True))
    alice, bob = sign_in(app, "Alice"), sign_in(app, "Bob")
    experiment = create_experiment(alice)
    # A new API process has an empty cache and must read the published document.
    control = PlatformControl(repository(client), control.artifacts, workspace=WORKSPACE)
    app = create_app(control=control, auth=Auth(secret="test-private-session-key-at-least-32-bytes", dev_auth=True))
    alice, bob = sign_in(app, "Alice"), sign_in(app, "Bob")

    def forbidden(*args, **kwargs):
        raise AssertionError("hosted catalogue requests must not discover games, construct SQL, or publish")

    monkeypatch.setattr(control, "compiler", forbidden)
    monkeypatch.setattr(ControlPlane, "__init__", forbidden)
    monkeypatch.setattr(catalogue, "installed_manifest", forbidden)
    monkeypatch.setattr(control.repo, "create", forbidden)
    writes_before = len(client.writes)
    reads_before = len([key for key in client.reads if key[0] == "groundwork_catalogues"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        adapters = list(pool.map(lambda _: control.catalogue, range(8)))
    assert all(adapter is adapters[0] for adapter in adapters)
    for _ in range(2):
        assert alice.get("/api/releases").status_code == 200
        environments = alice.get("/api/environments").json()["environments"]
        assert next(row for row in environments if row["environment_id"] == "buyer_seller")["experiment_count"] == 1
        assert all(row["experiment_count"] == 0 for row in bob.get("/api/environments").json()["environments"])
        for suffix in ("", "/items", "/workflow"):
            result = alice.get("/api/environments/buyer_seller" + suffix)
            assert result.status_code == 200, result.text
        assert alice.get("/api/agent-pool").status_code == 200
        response = alice.post("/api/designs/validate", json={"release_id": experiment["release_id"],
            "design_text": experiment["design_text"]})
        assert response.status_code == 200 and response.json()["valid"]
        assert bob.get(f"/api/experiments/{experiment['id']}").status_code == 404
    assert len(client.writes) == writes_before
    assert len([key for key in client.reads if key[0] == "groundwork_catalogues"]) == reads_before + 1
    assert control.catalogue.releases()[0].source_ref == "test-worker"
    # Publishing a newer snapshot cannot replace this revision's catalogue.
    changed = copy.deepcopy(published["manifest"])
    changed["environments"][0]["declaration"]["blurb"] += " New revision."
    second = FirestoreCatalogueStore(repository(client)).publish(changed)
    assert second["catalogue_sha256"] != published["catalogue_sha256"]
    assert control.catalogue.document == published


@pytest.mark.parametrize("pin", ["", "latest", "0" * 64])
def test_invalid_or_missing_hosted_pin_never_falls_back_to_local(tmp_path, monkeypatch, pin):
    monkeypatch.setenv("PLATFORM_CATALOGUE_SHA256", pin)
    def forbidden(*args, **kwargs):
        raise AssertionError("missing hosted metadata cannot fall back to discovery")
    monkeypatch.setattr(catalogue, "installed_manifest", forbidden)
    control = PlatformControl(repository(FirestoreClient()), ArtifactStore(backend="local", data_dir=tmp_path), workspace=WORKSPACE)
    with pytest.raises(ValueError, match="pin|missing"):
        control.releases()
    assert control._catalogue is None


def test_publication_permission_and_size_failures_leave_no_success_pin(tmp_path, monkeypatch):
    client = FirestoreClient()
    repo = repository(client)
    manifest = installed_manifest(WORKSPACE)
    oversized = copy.deepcopy(manifest)
    oversized["environments"][0]["declaration"]["blurb"] = "x" * 900_001
    with pytest.raises(ValueError, match="snapshot limit"):
        FirestoreCatalogueStore(repo).publish(oversized)
    assert not client.writes
    source, output = tmp_path / "manifest.json", tmp_path / "pin"
    source.write_text(json.dumps(manifest))
    def denied(*args, **kwargs):
        raise PermissionDenied("publisher role not granted")
    monkeypatch.setattr(repo, "create", denied)
    monkeypatch.setattr(publish_catalogue, "Repository", lambda **kwargs: repo)
    monkeypatch.setattr("sys.argv", ["publish", "--project", "test", "--manifest", str(source), "--output", str(output)])
    with pytest.raises(PermissionDenied):
        publish_catalogue.main()
    assert not output.exists()


def test_publisher_cli_writes_verified_hash_for_deployment(tmp_path, monkeypatch):
    client = FirestoreClient()
    repo = repository(client)
    manifest = installed_manifest(WORKSPACE)
    source, output = tmp_path / "manifest.json", tmp_path / "build" / "pin"
    source.write_text(json.dumps(manifest))
    monkeypatch.setattr(publish_catalogue, "Repository", lambda **kwargs: repo)
    monkeypatch.setattr("sys.argv", ["publish", "--project", "test", "--manifest", str(source), "--output", str(output)])
    publish_catalogue.main()
    digest = output.read_text().strip()
    assert FirestoreCatalogueStore(repo).load(digest)["manifest"] == manifest
    assert client.writes == [("groundwork_catalogues", digest)]
