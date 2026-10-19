"""A failed attempt must never supply the release validation handoff."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.fixture
def jobs():
    root = Path(__file__).resolve().parents[3]
    return yaml.safe_load((root / ".github/workflows/platform.yml").read_text())["jobs"]


def test_only_successful_cloud_validation_uploads_a_release_handoff(jobs):
    steps = jobs["platform-stack"]["steps"]
    upload = next(step for step in steps if step.get("id") == "validated-images")
    assert upload["uses"] == "actions/upload-artifact@v4"
    assert upload["if"] == (
        "success() && github.event_name != 'pull_request' && "
        "vars.GCP_WORKLOAD_IDENTITY_PROVIDER != ''"
    )
    verify_index = next(
        index
        for index, step in enumerate(steps)
        if "deploy/validated_images.py verify" in step.get("run", "")
    )
    assert steps.index(upload) > verify_index
    assert set(upload["with"]["path"].splitlines()) == {
        ".platform-build/candidates.json",
        ".platform-build/validated-images.json",
    }
    assert upload["with"]["include-hidden-files"] is True
    assert upload["with"]["if-no-files-found"] == "error"
    # v4 lists the latest artifact for each name even for ID downloads. A
    # distinct name per attempt keeps the producer ID visible across reruns.
    assert upload["with"]["name"] == "validated-images-${{ github.run_attempt }}"


def test_deploy_downloads_the_successful_producer_id_directly_into_the_handoff_path(jobs):
    assert jobs["platform-stack"]["outputs"]["validated-artifact-id"] == (
        "${{ steps.validated-images.outputs.artifact-id }}"
    )
    steps = jobs["deploy"]["steps"]
    download = next(step for step in steps if step.get("uses") == "actions/download-artifact@v4")
    assert download["with"] == {
        "artifact-ids": "${{ needs.platform-stack.outputs.validated-artifact-id }}",
        "path": ".platform-build",
        # v4 otherwise creates a subdirectory for ID-based downloads.
        "merge-multiple": True,
    }
    guard = next(step for step in steps if step.get("name") == "Require the validation artifact ID")
    assert steps.index(guard) < steps.index(download)
    assert guard["env"]["VALIDATED_ARTIFACT_ID"] == download["with"]["artifact-ids"]


@pytest.mark.parametrize("artifact_id, succeeds", [("", False), ("12186259194", True)])
def test_missing_producer_id_cannot_fall_back_to_downloading_all_artifacts(
    jobs, artifact_id, succeeds
):
    guard = next(
        step
        for step in jobs["deploy"]["steps"]
        if step.get("name") == "Require the validation artifact ID"
    )
    result = subprocess.run(
        ["bash", "-eu", "-c", guard["run"]],
        env={**os.environ, "VALIDATED_ARTIFACT_ID": artifact_id},
        capture_output=True,
    )
    assert (result.returncode == 0) is succeeds


def test_failure_diagnostics_are_separate_and_latency_download_uses_their_exact_id(jobs):
    stack = jobs["platform-stack"]
    diagnostics = next(step for step in stack["steps"] if step.get("id") == "build-diagnostics")
    assert diagnostics["if"] == "always()"
    assert diagnostics["with"]["name"] == "platform-build-diagnostics-${{ github.run_attempt }}"
    assert diagnostics["with"]["path"] == ".platform-build/*.json"
    assert diagnostics["with"]["include-hidden-files"] is True
    assert stack["outputs"]["diagnostics-artifact-id"] == (
        "${{ steps.build-diagnostics.outputs.artifact-id }}"
    )
    download = next(
        step
        for step in jobs["latencies"]["steps"]
        if step.get("uses") == "actions/download-artifact@v4"
    )
    assert download["if"] == "needs.platform-stack.outputs.diagnostics-artifact-id != ''"
    assert download["with"] == {
        "artifact-ids": "${{ needs.platform-stack.outputs.diagnostics-artifact-id }}",
        "path": "results/latencies",
        "merge-multiple": True,
    }
