"""Deployment preserves legacy pins and creates immutable infrastructure pairs."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from deploy import prepare_runtime, release
from deploy import common, verify_hosted

OLD_SOURCE, NEW_SOURCE = "a" * 40, "b" * 40
OLD_DIGEST, NEW_DIGEST, WORKER_DIGEST = ("sha256:" + value * 64 for value in ("1", "2", "3"))
OLD_IMAGE = "us-central1-docker.pkg.dev/project/groundwork/platform@" + OLD_DIGEST
NEW_IMAGE = "us-central1-docker.pkg.dev/project/groundwork-runtime/runtime@" + NEW_DIGEST
WORKER_IMAGE = "us-central1-docker.pkg.dev/project/groundwork/platform@" + WORKER_DIGEST


def legacy_job():
    return {"metadata": {"name": "groundwork-ep-legacy"}, "spec": {"template": {"spec": {"template": {"spec": {
        "containers": [{"image": OLD_IMAGE, "command": ["python"], "args": ["-m", "cloud_platform.worker"],
            "env": [{"name": key, "value": value} for key, value in {
                "PLATFORM_WORKSPACE": "/workspace", "PLATFORM_SOURCE_SHA": OLD_SOURCE,
                "PLATFORM_IMAGE_DIGEST": OLD_DIGEST}.items()]}]}}}}}}


def test_legacy_and_prior_paired_runtime_pins_survive_current_worker():
    legacy = legacy_job()
    prior_pair = release.job_manifest("project", "us-central1", WORKER_IMAGE, OLD_SOURCE,
        {"runtime_image": NEW_IMAGE, "runtime_source_sha": NEW_SOURCE})
    runtimes = release.discover_runtimes([legacy, prior_pair, {"metadata": {"name": "unrelated-app"}}], NEW_IMAGE, NEW_SOURCE)
    assert set(runtimes) == {OLD_DIGEST, NEW_DIGEST}
    catalog = release.binding_catalog(WORKER_IMAGE, NEW_SOURCE, runtimes)
    assert catalog["schema_version"] == 1 and catalog["worker"]["source_sha"] == NEW_SOURCE
    assert catalog["runtimes"][OLD_DIGEST]["runtime_image"] == OLD_IMAGE
    assert catalog["runtimes"][OLD_DIGEST]["runtime_source_sha"] == OLD_SOURCE
    assert catalog["runtimes"][OLD_DIGEST]["job_name"] == "groundwork-run-111111111111-333333333333"


@pytest.mark.parametrize("field,value", [("command", ["sh"]), ("image", "mutable:tag")])
def test_invalid_groundwork_candidate_cannot_silently_drop_old_pins(field, value):
    job = legacy_job()
    release.task_spec(job)["containers"][0][field] = value
    with pytest.raises(ValueError, match="Unsupported Groundwork Job"):
        release.discover_runtimes([job], NEW_IMAGE, NEW_SOURCE)


def test_pair_mounts_same_bridge_path_and_secrets_only_in_runtime():
    manifest = release.job_manifest("project", "us-central1", WORKER_IMAGE, NEW_SOURCE,
        {"runtime_image": OLD_IMAGE, "runtime_source_sha": OLD_SOURCE},
        [("OPENAI_API_KEY", "groundwork-openai-api-key")])
    task = release.task_spec(manifest)
    worker, runtime = task["containers"]
    assert worker["image"] == WORKER_IMAGE and runtime["image"] == OLD_IMAGE
    assert worker["volumeMounts"] == runtime["volumeMounts"] == [{"name": "execution", "mountPath": "/execution"}]
    assert not any(entry["name"].endswith("_API_KEY") for entry in worker["env"])
    assert next(entry for entry in runtime["env"] if entry["name"] == "OPENAI_API_KEY")["valueFrom"]["secretKeyRef"]["key"] == "latest"
    assert "cloud_platform" not in runtime["args"][1]
    assert task["maxRetries"] == 0 and task["timeoutSeconds"] == "3600"
    assert task["volumes"][0]["emptyDir"] == {"medium": "Memory", "sizeLimit": "512Mi"}


def test_existing_pair_rejects_wrong_worker_source_before_api_rollout():
    expected = release.job_manifest("project", "us-central1", WORKER_IMAGE, NEW_SOURCE,
        {"runtime_image": OLD_IMAGE, "runtime_source_sha": OLD_SOURCE})
    actual = copy.deepcopy(expected)
    for entry in release.task_spec(actual)["containers"][0]["env"]:
        if entry["name"] == "PLATFORM_SOURCE_SHA":
            entry["value"] = OLD_SOURCE
    with pytest.raises(ValueError, match="source/contract provenance"):
        release.validate_existing_pair(actual, expected)
    release.validate_existing_pair(expected, expected)


def runtime_cli(tmp_path, monkeypatch):
    tag = "us-central1-docker.pkg.dev/project/groundwork-runtime/runtime:" + NEW_SOURCE
    monkeypatch.setattr(sys, "argv", ["prepare_runtime.py", "--project", "project", "--image", tag,
        "--source-sha", NEW_SOURCE, "--output-dir", str(tmp_path)])
    return tag


def test_existing_runtime_source_tag_is_reused_without_rebuild(tmp_path, monkeypatch):
    tag = runtime_cli(tmp_path, monkeypatch)
    monkeypatch.setattr(prepare_runtime, "command", lambda *args, **kwargs: json.dumps({"image_summary": {"digest": NEW_DIGEST}}))
    prepare_runtime.main()
    assert (tmp_path / "runtime-existing").is_file()
    assert (tmp_path / "runtime-image").read_text() == tag.rsplit(":", 1)[0] + "@" + NEW_DIGEST


def test_runtime_lookup_permission_failure_aborts_instead_of_overwriting(tmp_path, monkeypatch):
    runtime_cli(tmp_path, monkeypatch)
    def denied(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["gcloud"], stderr="PERMISSION_DENIED")
    monkeypatch.setattr(prepare_runtime, "command", denied)
    with pytest.raises(RuntimeError, match="refusing to rebuild or overwrite"):
        prepare_runtime.main()
    assert not (tmp_path / "runtime-image").exists()


def test_only_explicit_runtime_not_found_selects_build(tmp_path, monkeypatch):
    tag = runtime_cli(tmp_path, monkeypatch)
    def missing(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["gcloud"], stderr="NOT_FOUND: Requested entity was not found")
    monkeypatch.setattr(prepare_runtime, "command", missing)
    prepare_runtime.main()
    assert not (tmp_path / "runtime-existing").exists()
    assert (tmp_path / "runtime-image").read_text() == tag


def test_science_history_covers_copied_inputs_and_ci_tests_real_pair():
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile.runtime").read_text()
    assert "cloud_platform" not in dockerfile and "COPY deploy/" not in dockerfile and "COPY web/" not in dockerfile
    workflow = (root / ".github/workflows/platform.yml").read_text()
    for source in ("Dockerfile.runtime", "scripts/install_workspace.py", "a2a-engine", "a2a-agent", "a2a-judge", "expt-runner", "games", "experiments"):
        assert source in workflow.split("git log -1 --format=%H -- ")[1].split(')"')[0]
    assert "docker compose -f compose.platform.yml up --build --detach --wait" in workflow
    assert "deploy/verify_local.py --url http://localhost:8080" in workflow
    build = yaml.safe_load((root / "cloudbuild.yaml").read_text())
    assert "_RUNTIME_SOURCE_SHA" in build["substitutions"]
    assert next(step for step in build["steps"] if step["id"] == "resolve-runtime")


def test_hosted_verifier_reads_catalog_and_pair_without_executing(monkeypatch):
    runtime = {"runtime_image": OLD_IMAGE, "runtime_source_sha": OLD_SOURCE, "contract_version": 1}
    catalog = release.binding_catalog(WORKER_IMAGE, NEW_SOURCE, {OLD_DIGEST: runtime})
    manifest = release.job_manifest("project", "us-central1", WORKER_IMAGE, NEW_SOURCE, runtime)
    env = {"PLATFORM_SOURCE_SHA": NEW_SOURCE, "PLATFORM_IMAGE_DIGEST": WORKER_DIGEST,
        "PLATFORM_RUNTIME_CATALOG_KEY": "platform/runtime-bindings/test/catalog.json",
        "PLATFORM_RUNTIME_DIGEST": OLD_DIGEST, "PLATFORM_RUNTIME_SOURCE_SHA": OLD_SOURCE,
        "PLATFORM_RUNTIME_IMAGE": OLD_IMAGE}
    service = {"spec": {"template": {"spec": {"containers": [{"image": WORKER_IMAGE,
        "env": [{"name": key, "value": value} for key,value in env.items()]}]}}}}
    calls = []
    def read(arguments, **kwargs):
        calls.append(arguments)
        if arguments[:3] == ["run", "services", "describe"]:
            return json.dumps(service)
        if arguments[:2] == ["storage", "cat"]:
            return json.dumps(catalog)
        if arguments[:3] == ["run", "jobs", "describe"]:
            return json.dumps(manifest)
        raise AssertionError("Verifier attempted an unexpected non-read command")
    monkeypatch.setattr(common, "command", read)
    result = verify_hosted.verify_runtime_bindings("project", "us-central1", NEW_SOURCE)
    assert result["paired_job_count"] == 1 and result["jobs_executed"] == 0
    assert len(calls) == 3
    release.task_spec(manifest)["containers"][0]["env"].append({"name": "OPENAI_API_KEY", "value": "invalid-worker-mount"})
    with pytest.raises(AssertionError, match="provider credentials"):
        verify_hosted.verify_runtime_bindings("project", "us-central1", NEW_SOURCE)
