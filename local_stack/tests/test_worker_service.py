"""The viewer publishes work; a separate worker reads it without shared secrets."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from a2a_engine.artifacts import ArtifactRef, fetch_verified
from local_stack.control_plane import ControlPlane
from local_stack.launchers.worker_service import WorkerServiceLauncher, atomic_json

WORKSPACE = Path(__file__).resolve().parents[2]
DESIGN = (Path(__file__).parent / "buyer_seller_design_smoke.yaml").read_text()


def test_published_presence_is_boolean_and_a_missing_key_is_named(tmp_path, monkeypatch):
    launcher = WorkerServiceLauncher(WORKSPACE, artifact_root=tmp_path,
                                     credentials=["OPENAI_API_KEY"])
    monkeypatch.setenv("OPENAI_API_KEY", "not-for-the-viewer")
    assert launcher.credential_presence(["OPENAI_API_KEY"]) == {"OPENAI_API_KEY": False}
    atomic_json(launcher.root / "presence.json", {
        "updated_at": time.time(), "credentials": {"OPENAI_API_KEY": False},
    })
    assert launcher.credential_presence(["OPENAI_API_KEY"]) == {"OPENAI_API_KEY": False}
    assert "not-for-the-viewer" not in (launcher.root / "presence.json").read_text()


def test_unresolved_symbolic_credential_stops_live_launch_before_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "viewer-must-ignore-this")
    control = ControlPlane(tmp_path / "a2a.db", workspace=WORKSPACE,
                           artifact_root=tmp_path / "artifacts",
                           launcher_spec={"backend": "worker_service", "credentials": ["OPENAI_API_KEY"]})
    design = DESIGN.replace("kind: scripted\n    binding: seller-baseline",
                            "kind: llm\n    binding: gpt-mini")
    experiment = control.create_experiment(name="missing-worker-key", release_id="buyer_seller",
                                           design_text=design)
    control.lock_experiment(experiment.id, design_sha256=experiment.design_sha256)
    with pytest.raises(ValueError, match="missing worker credentials: OPENAI_API_KEY"):
        control.launch_experiment(experiment.id, mode="live")
    assert not list((tmp_path / "artifacts" / "dispatch").glob("*.request.json"))


def test_design_launch_without_game_discovery_is_consumed_by_separate_worker(tmp_path, monkeypatch):
    import local_stack.control_plane as module

    monkeypatch.setattr(module, "installed_environments", lambda: [])
    monkeypatch.setattr(module, "get_environment_spec", lambda name: (_ for _ in ()).throw(AssertionError(name)))
    data = tmp_path / "data"
    data.mkdir()
    artifacts = data / "artifacts"
    worker = subprocess.Popen(
        [sys.executable, "-m", "expt_runner.worker_service", "--workspace", str(WORKSPACE),
         "--data", str(data), "--artifact-root", str(artifacts)], cwd=WORKSPACE,
        env={**os.environ, "PYTHONPATH": str(WORKSPACE)},
    )
    try:
        deadline = time.monotonic() + 10
        while not (artifacts / "dispatch/presence.json").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert worker.poll() is None
        control = ControlPlane(data / "a2a.db", workspace=WORKSPACE, artifact_root=artifacts,
                               launcher_spec={"backend": "worker_service", "credentials": []})
        assert "buyer_seller" in {release.environment_id for release in control.releases()}
        assert control.validate_design_text(release_id="buyer_seller", design_text=DESIGN)["valid"]
        experiment = control.create_experiment(name="separate-worker", release_id="buyer_seller",
                                               design_text=DESIGN)
        control.lock_experiment(experiment.id, design_sha256=experiment.design_sha256)
        launch = control.launch_experiment(experiment.id, mode="smoke")
        assert launch.provenance_grade == "unverified" and launch.image_digest is None
        ref = ArtifactRef(launch.launch_input_uri, launch.launch_input_sha256)
        plan = yaml.safe_load(fetch_verified(ref))
        assert plan["credentials"] == []
        queued = json.loads((artifacts / "dispatch" / f"{launch.id}.request.json").read_text()) if (
            artifacts / "dispatch" / f"{launch.id}.request.json").exists() else {}
        assert "OPENAI_API_KEY" not in json.dumps(queued)
        deadline = time.monotonic() + 30
        while control.launch(launch.id).status not in {"COMPLETED", "FAILED"} and time.monotonic() < deadline:
            time.sleep(0.1)
        assert control.launch(launch.id).status == "COMPLETED"
        assert control.progress(launch.id)["completed"] == 2
        assert all(row["provenance_grade"] == "unverified" for row in
                   control._store().episode_summaries({"experiment_id": experiment.id})[0])
    finally:
        worker.terminate()
        worker.wait(timeout=5)
