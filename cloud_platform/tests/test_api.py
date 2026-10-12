"""Real local runner evidence and control API authorization boundaries."""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from cloud_platform.api import create_app
from cloud_platform.auth import Auth
from cloud_platform.control import PlatformControl
from cloud_platform.storage import ArtifactStore, Repository
from cloud_platform.worker import Worker

WORKSPACE = Path(__file__).resolve().parents[2]


@pytest.fixture
def platform(tmp_path, monkeypatch):
    monkeypatch.setenv("PLATFORM_BACKEND", "local")
    monkeypatch.setenv("PLATFORM_SOURCE_SHA", "test-source")
    static_dir = tmp_path / "web"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html>Groundwork research control plane</html>")
    monkeypatch.setenv("PLATFORM_STATIC_DIR", str(static_dir))
    repository = Repository(backend="local", data_dir=tmp_path)
    artifacts = ArtifactStore(backend="local", data_dir=tmp_path)
    control = PlatformControl(repository, artifacts, workspace=WORKSPACE)
    auth = Auth(secret="test-private-session-key-at-least-32-bytes", dev_auth=True)
    application = create_app(control=control, auth=auth)
    worker = Worker(repository, artifacts, workspace=WORKSPACE)
    return application, control, worker


def sign_in(application, user):
    client = TestClient(application)
    config = client.get("/auth/config").json()
    client.headers["X-CSRF-Token"] = config["csrf_token"]
    response = client.post("/auth/dev", json={"user": user})
    assert response.status_code == 200, response.text
    return client


def create_experiment(client, *, design=True, name="Private experiment", owner_id="local:bob"):
    releases = client.get("/api/releases").json()["releases"]
    release = next(row for row in releases if row["environment_id"] == ("buyer_seller" if design else "negotiation"))
    body = {"name": name, "release_id": release["id"], "owner_id": owner_id}
    if design:
        body["design_text"] = (WORKSPACE / "local_stack/tests/buyer_seller_design_smoke.yaml").read_text()
    else:
        body["yaml_path"] = "games/negotiation/experiments/smoke_local.yaml"
    response = client.post("/api/experiments", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def test_unauthenticated_api_and_mutation_csrf(platform):
    application, _, _ = platform
    unauthenticated = TestClient(application)
    assert unauthenticated.get("/").status_code == 200
    assert unauthenticated.get("/api/health").status_code == 200
    for path in ("/api/releases", "/api/experiments", "/api/experiments/unknown", "/api/launches/unknown", "/api/episodes/unknown", "/api/exports/unknown/download"):
        assert unauthenticated.get(path).status_code == 401
    assert unauthenticated.post("/api/experiments", json={}).status_code == 401
    alice = sign_in(application, "Alice")
    alice.headers.pop("X-CSRF-Token")
    assert alice.post("/api/experiments", json={}).status_code == 403
    assert alice.post("/auth/logout", json={}).status_code == 403


@pytest.mark.parametrize("design", [True, False], ids=["locked-design", "checked-in-template"])
def test_private_experiment_executes_and_exports_joined_evidence(platform, design):
    application, control, worker = platform
    alice = sign_in(application, "Alice")
    experiment = create_experiment(alice, design=design)
    assert experiment["owner_id"] == "local:alice"
    assert [row["id"] for row in alice.get("/api/experiments").json()["experiments"]] == [experiment["id"]]
    admitted = alice.post(f"/api/experiments/{experiment['id']}/launch", json={"mode": "smoke", "max_parallelism": 1})
    assert admitted.status_code == 202, admitted.text
    launch = admitted.json()
    assert worker.run_once(launch["id"]) is True
    assert worker.run_once(launch["id"]) is False
    completed = alice.get(f"/api/launches/{launch['id']}").json()
    assert completed["status"] == "COMPLETED", completed
    assert completed["evidence_complete"] is True
    assert completed["attempts"] and all(row["status"] == "COMPLETED" for row in completed["attempts"])
    episodes = control.repo.list("episodes", owner_id="local:alice", experiment_id=experiment["id"])
    assert len(episodes) == len(completed["attempts"])
    for episode in episodes:
        trace = alice.get(f"/api/episodes/{episode['id']}")
        assert trace.status_code == 200
        assert trace.json()["events"]
    exported = alice.post(f"/api/experiments/{experiment['id']}/export", json={})
    assert exported.status_code == 201, exported.text
    export = exported.json()
    downloaded = alice.get(export["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.headers["Content-Type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as archive:
        metadata = json.loads(archive.read("metadata.json"))
        assert metadata["experiment"]["id"] == experiment["id"]
        assert metadata["experiment"]["owner_id"] == "local:alice"
        assert metadata["launches"][0]["id"] == launch["id"]
        assert metadata["attempts"] and metadata["episodes"]
        assert "metrics.csv" in archive.namelist()
        assert f"launches/{launch['id']}/trace.sqlite" in archive.namelist()
        assert f"experiments/{experiment['id']}/input.yaml" in archive.namelist()
        checksums = json.loads(archive.read("checksums.json"))
        assert set(checksums) == set(archive.namelist()) - {"checksums.json"}
        for path, digest in checksums.items():
            assert hashlib.sha256(archive.read(path)).hexdigest() == digest
    bob = sign_in(application, "Bob")
    assert bob.get("/api/experiments").json() == {"experiments": []}
    private_paths = [f"/api/experiments/{experiment['id']}", f"/api/launches/{launch['id']}",
                     export["download_url"]] + [f"/api/episodes/{row['id']}" for row in episodes]
    for path in private_paths:
        response = bob.get(path)
        assert response.status_code == 404, response.text
        assert response.json() == {"error": "Record not found"}
    for path in (f"/api/experiments/{experiment['id']}/launch", f"/api/experiments/{experiment['id']}/export", f"/api/launches/{launch['id']}/cancel"):
        assert bob.post(path, json={"mode": "smoke"}).status_code == 404
    bob_experiment = create_experiment(bob, name="Bob's own", owner_id="local:alice")
    assert bob_experiment["owner_id"] == "local:bob"
    assert [row["id"] for row in bob.get("/api/experiments").json()["experiments"]] == [bob_experiment["id"]]
    assert alice.get(f"/api/experiments/{bob_experiment['id']}").status_code == 404


def test_template_path_is_allowlisted(platform):
    application, _, _ = platform
    alice = sign_in(application, "Alice")
    release = alice.get("/api/releases").json()["releases"][0]
    response = alice.post("/api/experiments", json={"name": "Read secret", "release_id": release["id"],
                                                  "yaml_path": "../../.config/gcloud/credentials.db"})
    assert response.status_code == 400
    assert "checked-in" in response.json()["error"]


def test_repeat_attempts_keep_prior_evidence_and_release_pin(platform, monkeypatch):
    application, control, worker = platform
    alice = sign_in(application, "Alice")
    runtime_source = "1" * 40
    runtime_digest = "sha256:" + "1" * 64
    control.runtime_source_sha = runtime_source
    control.runtime_image_digest = runtime_digest
    control.runtime_image = "registry/runtime@" + runtime_digest
    control.source_sha, control.image_digest = "a" * 40, "sha256:" + "a" * 64

    def install_catalog(job):
        key = "platform/runtime-bindings/" + control.image_digest + "/catalog.json"
        catalog = {"schema_version": 1,
            "worker": {"source_sha": control.source_sha, "image_digest": control.image_digest,
                       "image": "registry/platform@" + control.image_digest},
            "runtimes": {runtime_digest: {"runtime_image": control.runtime_image,
                "runtime_source_sha": runtime_source, "job_name": job, "contract_version": 1}}}
        control.artifacts.put(key, json.dumps(catalog).encode())
        control.runtime_catalog_key, control._runtime_catalog = key, None

    install_catalog("worker-a-runtime-one")
    experiment = create_experiment(alice, design=True)
    frozen_plan = control.artifacts.get(experiment["frozen_plan_key"])
    # Current scientific backfills must never run while materializing or
    # inspecting an admitted execution of the old locked snapshot.
    def forbidden_backfill(*_):
        raise AssertionError("Current science attempted to rewrite a frozen design")
    monkeypatch.setattr("local_stack.control_plane.ControlPlane._backfill_compatible_word_guess_roles", forbidden_backfill)
    first_response = alice.post(f"/api/experiments/{experiment['id']}/launch", json={"mode": "smoke"})
    assert first_response.status_code == 202, first_response.text
    first = first_response.json()
    assert first["worker_source_sha"] == "a" * 40
    monkeypatch.setenv("PLATFORM_WORKER_SOURCE_SHA", first["worker_source_sha"])
    monkeypatch.setenv("PLATFORM_WORKER_IMAGE_DIGEST", first["worker_image_digest"])
    assert worker.run_once(first["id"])
    first_episodes = control.repo.list("episodes", owner_id="local:alice", experiment_id=experiment["id"])
    assert first_episodes and all(row["attempt"] == 1 for row in first_episodes)
    # Infrastructure and the current science release can both advance. The
    # existing experiment still runs its original science with the new worker.
    control.source_sha, control.image_digest = "b" * 40, "sha256:" + "b" * 64
    install_catalog("worker-b-runtime-one")
    control.runtime_source_sha = "2" * 40
    control.runtime_image_digest = "sha256:" + "2" * 64
    control.runtime_image = "registry/runtime@" + control.runtime_image_digest
    second_response = alice.post(f"/api/experiments/{experiment['id']}/launch", json={"mode": "smoke"})
    assert second_response.status_code == 202, second_response.text
    second = second_response.json()
    assert second["job_name"] == "worker-b-runtime-one"
    assert second["image_digest"] == second["runtime_image_digest"] == runtime_digest
    assert second["source_sha"] == second["runtime_source_sha"] == runtime_source
    assert second["worker_source_sha"] == "b" * 40
    assert second["worker_image_digest"] == "sha256:" + "b" * 64
    assert second["live_logs"] is True
    assert control.artifacts.get(experiment["frozen_plan_key"]) == frozen_plan
    saved = control.repo.get("experiments", experiment["id"])
    assert saved["runtime_image_digest"] == runtime_digest
    assert control.repo.get("launches", first["id"])["worker_source_sha"] == "a" * 40
    monkeypatch.setenv("PLATFORM_WORKER_SOURCE_SHA", second["worker_source_sha"])
    monkeypatch.setenv("PLATFORM_WORKER_IMAGE_DIGEST", second["worker_image_digest"])
    assert worker.run_once(second["id"])
    all_episodes = control.repo.list("episodes", owner_id="local:alice", experiment_id=experiment["id"])
    assert len(all_episodes) == len(first_episodes) * 2
    assert {row["attempt"] for row in all_episodes} == {1, 2}
    assert {row["episode_id"] for row in all_episodes if row["attempt"] == 1} == {
        row["episode_id"] for row in all_episodes if row["attempt"] == 2}
    assert {row["id"] for row in first_episodes}.issubset({row["id"] for row in all_episodes})
    assert alice.get(f"/api/launches/{second['id']}").json()["status"] == "COMPLETED"


def test_export_size_preflight_rejects_before_artifact_download(platform, monkeypatch):
    application, control, _ = platform
    alice = sign_in(application, "Alice")
    experiment = create_experiment(alice)
    monkeypatch.setattr(control.artifacts, "size", lambda key: 101 * 1024 * 1024)
    def forbidden_read(key):
        raise AssertionError("Export downloaded an oversized artifact before checking limits")
    monkeypatch.setattr(control.artifacts, "get", forbidden_read)
    response = alice.post(f"/api/experiments/{experiment['id']}/export", json={})
    assert response.status_code == 400
    assert "100 MiB" in response.json()["error"]
    assert control.repo.list("exports", owner_id="local:alice") == []


@pytest.mark.parametrize("design", ["parameters:\n  nested:\n    api_key: secret-value", "parameters:\n  access_token: secret-value", "[invalid"])
def test_design_cannot_embed_credentials_or_invalid_yaml(platform, design):
    application, _, _ = platform
    alice = sign_in(application, "Alice")
    release = alice.get("/api/releases").json()["releases"][0]
    response = alice.post("/api/experiments", json={"name": "Unsafe design", "release_id": release["id"],
                                                  "design_text": design})
    assert response.status_code == 400
    assert "secret-value" not in response.text


@pytest.mark.parametrize("mode,expected_episodes", [("smoke", 1), ("live", 2)])
def test_trusted_external_template_runs_agent_child_processes(platform, mode, expected_episodes):
    application, control, worker = platform
    alice = sign_in(application, "Alice")
    releases = alice.get("/api/releases").json()["releases"]
    release = next(row for row in releases if row["environment_id"] == "buyer_seller")
    response = alice.post("/api/experiments", json={"name": "Colocated trusted agents", "release_id": release["id"],
                                                   "yaml_path": "games/buyer-seller/experiments/runtime_smoke.yaml"})
    assert response.status_code == 201, response.text
    experiment = response.json()
    assert experiment["platform_policy"]["runtime_adaptations"] == [
        {"participant_id": "buyer", "from_runtime": "external", "to_runtime": "local_process"},
        {"participant_id": "seller", "from_runtime": "external", "to_runtime": "local_process"}]
    frozen = json.loads(control.artifacts.get(experiment["frozen_plan_key"]))
    assert all(agent["runtime"] == "local_process" for config in frozen for agent in config["agents"])
    original = control.artifacts.get(f"experiments/{experiment['id']}/input.yaml")
    assert b"runtime: external" in original
    admitted = alice.post(f"/api/experiments/{experiment['id']}/launch", json={"mode": mode})
    assert admitted.status_code == 202, admitted.text
    launch = admitted.json()
    assert worker.run_once(launch["id"])
    completed = alice.get(f"/api/launches/{launch['id']}").json()
    assert completed["status"] == "COMPLETED", completed
    episodes = control.repo.list("episodes", owner_id="local:alice", experiment_id=experiment["id"])
    assert len(episodes) == expected_episodes
    assert all(row["status"] == "COMPLETED" for row in episodes)
    for episode in episodes:
        trace = alice.get(f"/api/episodes/{episode['id']}").json()
        assert trace["events"]
        assert all(agent["runtime"] == "local_process" for agent in trace["config"]["agents"])


def test_user_design_external_runtime_is_rejected(platform):
    application, _, _ = platform
    alice = sign_in(application, "Alice")
    releases = alice.get("/api/releases").json()["releases"]
    release = next(row for row in releases if row["environment_id"] == "buyer_seller")
    design = (WORKSPACE / "local_stack/tests/buyer_seller_design_smoke.yaml").read_text()
    design = design.replace("kind: scripted", "kind: scripted\n    runtime: external")
    response = alice.post("/api/experiments", json={"name": "Unsupported external agents", "release_id": release["id"],
                                                   "design_text": design})
    assert response.status_code == 400
    assert "runtime" in response.json()["error"]


def test_researcher_image_preview_requires_session_and_verified_release_asset(platform, tmp_path, monkeypatch):
    import base64
    from a2a_engine.content import AssetStore
    from a2a_engine.items import ItemBank
    application, control, _ = platform
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=")
    image = AssetStore(tmp_path / "assets").put(png, "image/png")
    path = tmp_path / "items.jsonl"
    path.write_text(json.dumps({"item_id": "private-fixture", "params": {"content": [image.model_dump(exclude_none=True)]}}))
    bank = ItemBank.load(path)
    monkeypatch.setattr(control.catalogue, "_item_bank", lambda identifier: bank)
    url = f"/api/environments/calendar/assets/{image.sha256}"
    assert TestClient(application).get(url).status_code == 401
    client = sign_in(application, "Alice")
    response = client.get(url)
    assert response.status_code == 200 and response.content == png
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    assert client.get("/api/environments/calendar/assets/" + "0" * 64).status_code == 404
