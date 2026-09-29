"""A published release is sufficient for design work, not runtime dispatch."""

from __future__ import annotations

from pathlib import Path
import os
import json
import yaml

import pytest
import subprocess
import shutil
import time

from a2a_engine.compiler import compile, validate
from a2a_engine.items import ItemBank
from a2a_engine.registry import discover_environments, get_environment_spec
from a2a_engine.release_surface import PublishedRelease, project_surface, publish_release, surface_digest
from local_stack.control_plane import ControlPlane
from local_stack.launchers import ExecutionStatus
from local_stack.tests.fakes import FakeLauncher
from a2a_engine.storage.sqlite import SQLiteEpisodeStore


ROOT = Path(__file__).resolve().parents[2]
IDS = ("calendar", "buyer_seller", "negotiation", "word_guess")


@pytest.mark.parametrize("environment_id", IDS)
def test_published_surface_round_trips_and_matches_seeded_release(tmp_path, environment_id):
    discover_environments()
    spec = get_environment_spec(environment_id)
    manifest = publish_release(spec.declaration, package=spec.package)
    restored = PublishedRelease.model_validate_json(manifest.model_dump_json())
    assert restored.surface == project_surface(spec.declaration)
    assert restored.surface_sha256 == surface_digest(restored.surface)
    control = ControlPlane(tmp_path / "control.db", workspace=ROOT)
    seeded = control._release_for(environment_id, None)
    ingested = control.ingest_release(restored.model_dump(mode="json"))
    assert (ingested.id, ingested.environment_id, ingested.version, ingested.declaration_sha256,
            ingested.item_bank_sha256, ingested.oracle_version) == (
                seeded.id, seeded.environment_id, seeded.version, seeded.declaration_sha256,
                seeded.item_bank_sha256, seeded.oracle_version)
    assert ingested.id == spec.declaration.id
    assert ingested.declaration_sha256 == spec.declaration.content_sha256()
    assert ingested.item_bank_sha256 == spec.declaration.item_policy.item_bank_sha256
    assert ingested.oracle_version == spec.declaration.oracle_version


def test_corrupt_surface_digest_is_rejected(tmp_path):
    discover_environments()
    manifest = publish_release(get_environment_spec("calendar").declaration).model_dump(mode="json")
    manifest["surface"]["roles"][0]["count"] += 1
    control = ControlPlane(tmp_path / "control.db", workspace=ROOT)
    with pytest.raises(ValueError, match="surface digest mismatch"):
        control.ingest_release(manifest)


def test_locked_experiment_keeps_its_image_across_retries_and_rejects_republish(tmp_path, monkeypatch):
    discover_environments()
    declaration = get_environment_spec("buyer_seller").declaration
    first = "sha256:" + "1" * 64
    second = "sha256:" + "2" * 64
    control = ControlPlane(tmp_path / "control.db", workspace=ROOT)
    control.ingest_release(publish_release(declaration, image_digest=first).model_dump(mode="json"))
    monkeypatch.setattr(control, "seed_installed_releases", lambda: [])
    from local_stack.tests.test_design_api import DESIGN

    experiment = control.create_experiment(name="pinned", release_id="buyer_seller", design_text=DESIGN)
    experiment = control.lock_experiment(experiment.id, design_sha256=experiment.design_sha256)
    assert experiment.pinned_image_digest == first
    fake = FakeLauncher()
    fake.name = "local_container"
    control.launcher = fake
    initial = control.launch_experiment(experiment.id, mode="smoke")
    fake.finish(initial.id, ExecutionStatus.FAILED)
    control.reconcile()

    with pytest.raises(ValueError, match="publish a new release ID"):
        control.ingest_release(publish_release(declaration, image_digest=second).model_dump(mode="json"))
    assert control._release_by_id("buyer_seller").image_digest == first
    retry = control.launch_experiment(experiment.id, mode="smoke")
    assert retry.image_digest == initial.image_digest == first
    for launch, ref in fake.submitted:
        assert launch.image_digest == first
        assert ref.sha256
        plan = yaml.safe_load(Path(launch.execution_path).read_text())
        assert all(cell["config"]["provenance"]["image_digest"] == first
                   for cell in plan["cells"])


def test_lock_without_image_cannot_be_upgraded_by_later_publish(tmp_path, monkeypatch):
    discover_environments()
    declaration = get_environment_spec("buyer_seller").declaration
    control = ControlPlane(tmp_path / "control.db", workspace=ROOT)
    control.ingest_release(publish_release(declaration).model_dump(mode="json"))
    monkeypatch.setattr(control, "seed_installed_releases", lambda: [])
    from local_stack.tests.test_design_api import DESIGN

    experiment = control.create_experiment(name="local", release_id="buyer_seller", design_text=DESIGN)
    experiment = control.lock_experiment(experiment.id, design_sha256=experiment.design_sha256)
    assert experiment.pinned_image_digest == ""
    with pytest.raises(ValueError, match="publish a new release ID"):
        control.ingest_release(publish_release(declaration, image_digest="sha256:" + "a" * 64).model_dump(mode="json"))
    fake = FakeLauncher()
    fake.name = "local_container"
    control.launcher = fake
    launch = control.launch_experiment(experiment.id, mode="smoke")
    assert launch.image_digest is None and launch.provenance_grade == "unverified"


def test_existing_locked_experiment_migrates_pin_from_its_first_launch(tmp_path, monkeypatch):
    discover_environments()
    declaration = get_environment_spec("buyer_seller").declaration
    first = "sha256:" + "3" * 64
    control = ControlPlane(tmp_path / "control.db", workspace=ROOT)
    control.ingest_release(publish_release(declaration, image_digest=first).model_dump(mode="json"))
    monkeypatch.setattr(control, "seed_installed_releases", lambda: [])
    from local_stack.tests.test_design_api import DESIGN

    experiment = control.create_experiment(name="legacy-pin", release_id="buyer_seller", design_text=DESIGN)
    experiment = control.lock_experiment(experiment.id, design_sha256=experiment.design_sha256)
    fake = FakeLauncher(status=ExecutionStatus.FAILED)
    fake.name = "local_container"
    control.launcher = fake
    control.launch_experiment(experiment.id, mode="smoke")
    with control._session() as db:
        db.execute("ALTER TABLE experiments DROP COLUMN pinned_image_digest")
        db.execute("UPDATE releases SET image_digest = ? WHERE id = ?", ("sha256:" + "4" * 64, "buyer_seller"))
    migrated = ControlPlane(control.path, workspace=ROOT)
    assert migrated.experiment(experiment.id).pinned_image_digest == first


def test_mixed_legacy_images_cannot_acquire_a_false_single_pin(tmp_path, monkeypatch):
    discover_environments()
    declaration = get_environment_spec("buyer_seller").declaration
    first = "sha256:" + "5" * 64
    control = ControlPlane(tmp_path / "control.db", workspace=ROOT)
    control.ingest_release(publish_release(declaration, image_digest=first).model_dump(mode="json"))
    monkeypatch.setattr(control, "seed_installed_releases", lambda: [])
    from local_stack.tests.test_design_api import DESIGN

    experiment = control.create_experiment(name="mixed-legacy", release_id="buyer_seller", design_text=DESIGN)
    experiment = control.lock_experiment(experiment.id, design_sha256=experiment.design_sha256)
    fake = FakeLauncher(status=ExecutionStatus.FAILED)
    fake.name = "local_container"
    control.launcher = fake
    control.launch_experiment(experiment.id, mode="smoke")
    control.launch_experiment(experiment.id, mode="smoke")
    with control._session() as db:
        db.execute("UPDATE launches SET image_digest = ? WHERE id = ?",
                   ("sha256:" + "6" * 64, fake.submitted[-1][0].id))
        db.execute("ALTER TABLE experiments DROP COLUMN pinned_image_digest")
    migrated = ControlPlane(control.path, workspace=ROOT)
    assert migrated.experiment(experiment.id).pinned_image_digest is None
    with pytest.raises(ValueError, match="no pinned image identity"):
        migrated.launch_experiment(experiment.id, mode="smoke")


def test_manifest_and_declaration_compile_identically_and_reject_unknown_binding(tmp_path):
    discover_environments()
    spec = get_environment_spec("calendar")
    control = ControlPlane(tmp_path / "control.db", workspace=ROOT)
    release = control.ingest_release(publish_release(spec.declaration).model_dump(mode="json"))
    projected = control._design_declaration(release)
    bank = ItemBank.load(ROOT / spec.declaration.item_policy.bank_path)
    from calendar_game.tests.test_design_contract import _design

    good = _design("dsm")
    bad = _design("not-declared")
    assert validate(good, projected, bank) == validate(good, spec.declaration, bank)
    assert validate(bad, projected, bank) == validate(bad, spec.declaration, bank)
    assert compile(good, projected, bank, experiment_id="id", experiment_name="name") == compile(
        good, spec.declaration, bank, experiment_id="id", experiment_name="name")
    good_text = yaml.safe_dump(good.model_dump(mode="json", exclude_none=True))
    bad_text = yaml.safe_dump(bad.model_dump(mode="json", exclude_none=True))
    published_results = [control.validate_design_text(release_id=release.id, design_text=text)
                         for text in (good_text, bad_text)]
    with control._session() as db:
        db.execute("UPDATE releases SET manifest = NULL WHERE id = ?", (release.id,))
    imported_results = [control.validate_design_text(release_id=release.id, design_text=text)
                        for text in (good_text, bad_text)]
    assert published_results == imported_results
    assert published_results[0]["valid"] is True
    assert published_results[1]["valid"] is False


def test_subprocess_launch_is_unverified_even_with_published_image(tmp_path):
    control = ControlPlane(tmp_path / "control.db", workspace=ROOT)
    control.seed_installed_releases()
    from local_stack.tests.test_design_api import DESIGN

    experiment = control.create_experiment(name="unverified", release_id="buyer_seller", design_text=DESIGN)
    control.lock_experiment(experiment.id, design_sha256=experiment.design_sha256)
    launch = control.launch_experiment(experiment.id, mode="smoke")
    assert launch.provenance_grade == "unverified" and launch.image_digest is None
    for _ in range(100):
        if control.launch(launch.id).status in {"COMPLETED", "FAILED", "CANCELLED"}:
            break
        time.sleep(0.05)
        control.reconcile()
    assert control.launch(launch.id).status == "COMPLETED"
    attempts = control.launch_detail(launch.id)["attempts"]
    for attempt in attempts:
        trace = SQLiteEpisodeStore(path=control.path).get_episode(attempt["episode_uid"])
        assert trace.config.provenance["provenance_grade"] == "unverified"
        assert trace.config.provenance["image_digest"] is None


def test_failed_container_dispatch_does_not_claim_verified_execution(tmp_path, monkeypatch):
    discover_environments()
    declaration = get_environment_spec("buyer_seller").declaration
    manifest = publish_release(declaration, image_digest="sha256:" + "0" * 64)
    control = ControlPlane(tmp_path / "control.db", workspace=ROOT,
                           launcher_spec={"backend": "local_container"})
    control.ingest_release(manifest.model_dump(mode="json"))
    from local_stack.tests.test_design_api import DESIGN

    experiment = control.create_experiment(name="no-image", release_id="buyer_seller", design_text=DESIGN)
    control.lock_experiment(experiment.id, design_sha256=experiment.design_sha256)

    def unavailable(*_args):
        raise RuntimeError("Docker image unavailable")

    monkeypatch.setattr(control.launcher, "_docker", unavailable)
    with pytest.raises(RuntimeError, match="Docker image unavailable"):
        control.launch_experiment(experiment.id, mode="smoke")
    launches = control.launches()
    assert len(launches) == 1
    assert launches[0].status == "FAILED"
    assert launches[0].provenance_grade == "unverified"
    assert launches[0].image_digest is None


def test_digest_pinned_container_stamps_every_persisted_episode(tmp_path):
    manifest_path = ROOT / "games/buyer-seller/runtime/release.manifest.json"
    if not manifest_path.exists() or not shutil.which("docker"):
        pytest.skip("build the buyer-seller release image first")
    manifest = PublishedRelease.model_validate_json(manifest_path.read_text())
    if subprocess.run(["docker", "image", "inspect", manifest.image_digest],
                      capture_output=True, check=False).returncode:
        pytest.skip("pinned image unavailable on this host")
    # Docker Desktop's file-sharing VM does not inherit pytest's private 0700
    # ownership; SQLite needs directory write access for its WAL sidecars.
    os.chmod(tmp_path, 0o777)
    control = ControlPlane(tmp_path / "control.db", workspace=ROOT,
                           launcher_spec={"backend": "local_container"})
    control.ingest_release(manifest.model_dump(mode="json"))
    from local_stack.tests.test_design_api import DESIGN

    experiment = control.create_experiment(name="container-provenance", release_id="buyer_seller", design_text=DESIGN)
    control.lock_experiment(experiment.id, design_sha256=experiment.design_sha256)
    launch = control.launch_experiment(experiment.id, mode="smoke")
    assert launch.provenance_grade == "verified" and launch.image_digest == manifest.image_digest
    # Host/VM SQLite WAL file sharing is unsafe for concurrent readers on
    # Docker Desktop. Compose uses a Linux volume; this host integration test
    # waits for the container before asking the host control plane to read it.
    container_id = json.loads(launch.execution_handle)["detail"]["containers"][0]
    exit_code = subprocess.check_output(["docker", "wait", container_id], text=True).strip()
    assert exit_code == "0", subprocess.check_output(["docker", "logs", container_id], text=True)
    assert control.launch(launch.id).status == "COMPLETED"
    attempts = control.launch_detail(launch.id)["attempts"]
    for attempt in attempts:
        trace = SQLiteEpisodeStore(path=control.path).get_episode(attempt["episode_uid"])
        assert trace.config.provenance["image_digest"] == manifest.image_digest
        assert trace.config.provenance["provenance_grade"] == "verified"
    assert all(row["provenance_grade"] == "verified" for row in SQLiteEpisodeStore(path=control.path).episode_summaries({"experiment_id": experiment.id})[0])
    assert all(cell["completed_replicas"] == 0 for cell in control.cell_evidence(experiment.id))
