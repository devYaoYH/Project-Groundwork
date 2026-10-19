"""Deployment must promote tested content, including an older pinned runtime."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from deploy import validated_images as images

SOURCE, RUNTIME_SOURCE = "a" * 40, "b" * 40
PLATFORM_ID, RUNTIME_ID, PLATFORM_DIGEST, RUNTIME_DIGEST = (
    "sha256:" + value * 64 for value in "1234"
)
PLATFORM_REPO = "us-central1-docker.pkg.dev/project/groundwork/platform"
RUNTIME_REPO = "us-central1-docker.pkg.dev/project/groundwork-runtime/runtime"
PLATFORM_IMAGE = PLATFORM_REPO + "@" + PLATFORM_DIGEST
RUNTIME_IMAGE = RUNTIME_REPO + "@" + RUNTIME_DIGEST


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    path = tmp_path / "images.json"
    ids = dict(zip(images.LOCAL_IMAGES.values(), (PLATFORM_ID, RUNTIME_ID), strict=True))
    monkeypatch.setattr(images, "image_id", ids.__getitem__)
    images.record_images(path, SOURCE, RUNTIME_SOURCE)
    return path, ids


def test_failed_or_unfinished_validation_cannot_publish(recorded, monkeypatch):
    path, _ = recorded
    calls = []
    monkeypatch.setattr(images, "docker", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="finish successfully"):
        images.publish_images(path, "project", "us-central1", SOURCE, "123-1")
    assert not calls


@pytest.mark.parametrize("when", ["before_validation_finishes", "before_publication"])
def test_retagging_either_tested_image_aborts_before_any_push(recorded, monkeypatch, when):
    path, ids = recorded
    if when == "before_publication":
        images.verify_images(path, SOURCE)
    ids["groundwork-runtime:local"] = PLATFORM_ID
    calls = []
    monkeypatch.setattr(images, "docker", lambda *args: calls.append(args))
    with pytest.raises(ValueError, match="changed after validation"):
        if when == "before_publication":
            images.publish_images(path, "project", "us-central1", SOURCE, "123-1")
        else:
            images.verify_images(path, SOURCE)
    assert not calls


def test_validation_from_another_commit_cannot_be_reused(recorded):
    path, _ = recorded
    with pytest.raises(ValueError, match="different source revision"):
        images.verify_images(path, RUNTIME_SOURCE)


def registry(monkeypatch, *, runtime_exists=False, wrong_content=False):
    calls = []

    def lookup(project, tag):
        if (
            tag.startswith(RUNTIME_REPO)
            and not runtime_exists
            and not any(c[0] == "push" for c in calls)
        ):
            return None
        return RUNTIME_IMAGE if tag.startswith(RUNTIME_REPO) else PLATFORM_IMAGE

    def docker(*args):
        calls.append(args)
        if args[:2] == ("manifest", "inspect"):
            digest = RUNTIME_ID if args[-1] == RUNTIME_IMAGE else PLATFORM_ID
            return json.dumps({"config": {"digest": PLATFORM_DIGEST if wrong_content else digest}})
        return ""

    monkeypatch.setattr(images, "registry_image", lookup)
    monkeypatch.setattr(images, "docker", docker)
    return calls


def test_published_digests_reference_recorded_configs_and_never_rebuild(recorded, monkeypatch):
    path, _ = recorded
    images.verify_images(path, SOURCE)
    calls = registry(monkeypatch)
    result = images.publish_images(path, "project", "us-central1", SOURCE, "123-2")
    assert result == {
        "platform-image": PLATFORM_IMAGE,
        "runtime-image": RUNTIME_IMAGE,
        "source-sha": SOURCE,
        "runtime-source-sha": RUNTIME_SOURCE,
    }
    assert ("tag", PLATFORM_ID, PLATFORM_REPO + ":" + SOURCE + "-123-2") in calls
    assert ("tag", RUNTIME_ID, RUNTIME_REPO + ":" + RUNTIME_SOURCE) in calls
    assert sum(call[0] == "push" for call in calls) == 2
    assert all(call[0] in {"tag", "push", "manifest"} for call in calls)
    assert json.loads(path.read_text())["published"] == result


def test_old_science_digest_is_validated_and_reused_without_push(recorded, monkeypatch):
    path, _ = recorded
    record = json.loads(path.read_text())
    record["reused_runtime"] = RUNTIME_IMAGE
    images.write_record(path, record)
    images.verify_images(path, SOURCE)
    calls = registry(monkeypatch)
    result = images.publish_images(path, "project", "us-central1", SOURCE, "123-1")
    assert result["runtime-image"] == RUNTIME_IMAGE
    assert result["runtime-source-sha"] == RUNTIME_SOURCE
    assert not any(call[0] in {"push", "tag"} and RUNTIME_REPO in call[-1] for call in calls)


def test_concurrent_science_publication_is_reused_only_if_tested_content_matches(
    recorded, monkeypatch
):
    path, _ = recorded
    images.verify_images(path, SOURCE)
    calls = registry(monkeypatch, runtime_exists=True, wrong_content=True)
    with pytest.raises(ValueError, match="differs from the image"):
        images.publish_images(path, "project", "us-central1", SOURCE, "123-1")
    assert not any(call[0] in {"push", "tag"} for call in calls)
    assert "published" not in json.loads(path.read_text())


def test_registry_lookup_errors_do_not_authorize_replacement(monkeypatch):
    def denied(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["gcloud"], stderr="PERMISSION_DENIED")

    monkeypatch.setattr(images, "command", denied)
    with pytest.raises(RuntimeError, match="refusing to replace"):
        images.registry_image("project", RUNTIME_REPO + ":" + RUNTIME_SOURCE)


@pytest.mark.parametrize(
    "field,value",
    [
        ("validated_source_sha", RUNTIME_SOURCE),
        ("platform_image", PLATFORM_REPO + ":latest"),
        ("runtime_image", RUNTIME_REPO + ":" + RUNTIME_SOURCE),
        ("runtime_image", PLATFORM_IMAGE),
        ("platform_image", PLATFORM_IMAGE.replace("/project/", "/another-project/")),
        ("runtime_source_sha", "main"),
        ("platform_image", ""),
    ],
)
def test_deployment_rejects_source_skew_mutable_images_or_wrong_namespace(field, value):
    args = dict(
        project="project",
        region="us-central1",
        source_sha=SOURCE,
        validated_source_sha=SOURCE,
        platform_image=PLATFORM_IMAGE,
        runtime_image=RUNTIME_IMAGE,
        runtime_source_sha=RUNTIME_SOURCE,
    )
    images.check_release(**args)
    args[field] = value
    with pytest.raises(ValueError):
        images.check_release(**args)


def test_workflow_validates_selected_images_before_publishing_and_passes_digests():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/platform.yml").read_text())
    jobs = workflow["jobs"]
    stack = jobs["platform-stack"]["steps"]
    names = [step.get("name", "") for step in stack]
    assert names.index("Select the science image before validation") < names.index(
        "Record the images entering validation"
    )
    assert names.index("Record the images entering validation") < names.index(
        "Verify the paired local worker and science runtime"
    )
    assert names.index("Verify the paired local worker and science runtime") < names.index(
        "Test the release images without rebuilding"
    )
    assert names.index("Test the release images without rebuilding") < names.index(
        "Verify validation did not replace either image"
    )
    assert names.index("Verify validation did not replace either image") < names.index(
        "Publish the validated images"
    )
    for step in stack:
        if step.get("id") in {"runtime", "publish"} or step.get("uses", "").startswith(
            "google-github-actions/"
        ):
            assert "github.event_name != 'pull_request'" in step["if"]
    deploy = jobs["deploy"]
    assert set(deploy["needs"]) == {"validate", "platform-stack"}
    command = deploy["steps"][-1]
    for field in (
        "platform-image",
        "runtime-image",
        "runtime-source-sha",
        "source-sha",
    ):
        assert "${{ needs.platform-stack.outputs." + field + " }}" in command["env"].values()
    assert "validated_images.py check" in command["run"]


def test_registry_replacement_after_push_aborts_without_a_deployment_record(recorded, monkeypatch):
    path, _ = recorded
    images.verify_images(path, SOURCE)
    calls = registry(monkeypatch, wrong_content=True)
    with pytest.raises(ValueError, match="differs from the image"):
        images.publish_images(path, "project", "us-central1", SOURCE, "123-1")
    assert any(call[0] == "push" for call in calls)
    assert "published" not in json.loads(path.read_text())
