"""Cloud candidates must remain the same artifacts across GitHub validation and publication."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from deploy import cloud_images as cloud
from deploy import latency
from deploy import validated_images as images

SOURCE, SCIENCE, BUILD = "a" * 40, "b" * 40, "12345678-1234-1234-1234-123456789012"
CONFIG, SCI_CONFIG = ("sha256:" + value * 64 for value in "12")
DIGEST, SCI_DIGEST = ("sha256:" + value * 64 for value in "34")
PLATFORM = "us-central1-docker.pkg.dev/project/groundwork/platform@" + DIGEST
CANDIDATE = "us-central1-docker.pkg.dev/project/groundwork/candidate-runtime@" + SCI_DIGEST
RUNTIME = "us-central1-docker.pkg.dev/project/groundwork-runtime/runtime@" + SCI_DIGEST


@pytest.fixture
def candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud, "ROOT", tmp_path)
    record = dict(
        schema_version=1,
        source_sha=SOURCE,
        runtime_source_sha=SCIENCE,
        build_id=BUILD,
        reused_runtime="",
        images={"platform": PLATFORM, "runtime": CANDIDATE},
        configs={"platform": CONFIG, "runtime": SCI_CONFIG},
    )
    images.write_record(tmp_path / "candidates.json", record)
    return record


@pytest.mark.parametrize("existing", [None, RUNTIME])
def test_cloud_build_has_one_cache_writer_per_new_image_and_selects_old_science_first(
    candidates,
    monkeypatch,
    existing,
):
    monkeypatch.setattr(images, "registry_image", lambda *a: existing)
    monkeypatch.setattr(cloud, "command", lambda *a: "")
    cloud.prepare("project", "us-central1", SOURCE, SCIENCE, BUILD)
    bake = json.loads((cloud.ROOT / "cloud-bake.json").read_text())
    assert set(bake["target"]) == ({"platform"} if existing else {"platform", "runtime"})
    for name, target in bake["target"].items():
        assert len(target["cache-to"]) == len(target["cache-from"]) == 1
        assert "/groundwork/build-cache:" + name in target["cache-to"][0]
        assert "mode=max" in target["cache-to"][0]
        assert target["platforms"] == ["linux/amd64"]
    assert json.loads((cloud.ROOT / "build-inputs.json").read_text())["reused_runtime"] == (
        existing or ""
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_sha", SCIENCE),
        ("runtime_source_sha", SOURCE),
        ("build_id", "another-build"),
        ("schema_version", 2),
        ("reused_runtime", RUNTIME),
        ("images", {"platform": PLATFORM, "runtime": PLATFORM}),
        ("configs", {"platform": CONFIG, "runtime": "mutable"}),
    ],
)
def test_download_refuses_wrong_build_source_or_namespace(candidates, field, value):
    candidates[field] = value
    with pytest.raises(ValueError):
        cloud.check_candidates(candidates, "project", "us-central1", SOURCE, SCIENCE, BUILD)


@pytest.mark.parametrize("wrong_download", [False, True])
def test_pull_by_digest_records_only_the_downloaded_candidate_configs(
    candidates, monkeypatch, wrong_download
):
    calls = []
    monkeypatch.setattr(cloud, "command", lambda *a: "")
    monkeypatch.setattr(cloud.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    monkeypatch.setattr(images, "docker", lambda *a: calls.append(list(a)))
    monkeypatch.setattr(
        images,
        "image_id",
        lambda ref: (
            CONFIG
            if wrong_download or ref in (PLATFORM, images.LOCAL_IMAGES["platform"])
            else SCI_CONFIG
        ),
    )
    if wrong_download:
        with pytest.raises(ValueError, match="Downloaded image differs"):
            cloud.pull("project", "us-central1", SOURCE, SCIENCE, BUILD)
        assert not (cloud.ROOT / "validated-images.json").exists()
    else:
        cloud.pull("project", "us-central1", SOURCE, SCIENCE, BUILD)
        assert ["docker", "pull", PLATFORM] in calls
        assert ["docker", "pull", CANDIDATE] in calls
        record = json.loads((cloud.ROOT / "validated-images.json").read_text())
        assert record["images"] == candidates["configs"]
        assert record["validated"] is False


@pytest.mark.parametrize("failure", ["", "unfinished", "source", "config"])
def test_handoff_from_separate_runner_requires_successful_matching_validation(candidates, failure):
    record = dict(
        schema_version=1,
        source_sha=SOURCE,
        runtime_source_sha=SCIENCE,
        images=candidates["configs"].copy(),
        validated=True,
    )
    if failure == "unfinished":
        record["validated"] = False
    elif failure == "source":
        record["source_sha"] = SCIENCE
    elif failure == "config":
        record["images"]["runtime"] = CONFIG
    images.write_record(cloud.ROOT / "validated-images.json", record)
    if failure:
        with pytest.raises(ValueError):
            cloud.handoff("project", "us-central1", SOURCE)
    else:
        output = cloud.handoff("project", "us-central1", SOURCE)
        assert output["_RUNTIME_IMAGE"] == CANDIDATE
        assert output["_RUNTIME_CONFIG_ID"] == SCI_CONFIG


@pytest.mark.parametrize("existing", [None, RUNTIME, PLATFORM])
def test_promotion_copies_exact_science_or_rejects_a_concurrent_conflict(
    candidates, monkeypatch, existing
):
    calls = []
    monkeypatch.setattr(cloud, "command", lambda *a: "")
    monkeypatch.setattr(
        images, "registry_image", lambda *a: RUNTIME if ("copy",) in calls else existing
    )

    def verify(image, config):
        if image == PLATFORM and config == SCI_CONFIG:
            raise ValueError("Published image differs from the image that passed validation")
        calls.append(("verify", image, config))

    monkeypatch.setattr(images, "verify_registry_image", verify)
    monkeypatch.setattr(images, "image_id", lambda *a: SCI_CONFIG)
    monkeypatch.setattr(cloud.subprocess, "run", lambda args, **k: calls.append(tuple(args)))
    monkeypatch.setattr(
        images,
        "docker",
        lambda *a: (
            calls.append(("copy",))
            if a[:3] == ("buildx", "imagetools", "create") and "--prefer-index=false" in a
            else pytest.fail("Unexpected Docker operation")
        ),
    )
    arguments = (
        "project",
        "us-central1",
        SOURCE,
        SOURCE,
        PLATFORM,
        CANDIDATE,
        SCIENCE,
        CONFIG,
        SCI_CONFIG,
    )
    if existing == PLATFORM:
        with pytest.raises(ValueError, match="differs from"):
            cloud.promote(*arguments)
        assert not (cloud.ROOT / "promoted-runtime").exists()
        assert ("copy",) not in calls
    else:
        cloud.promote(*arguments)
        assert (cloud.ROOT / "promoted-runtime").read_text() == RUNTIME
        assert (("copy",) in calls) is (existing is None)
    assert not any("build" in call for call in calls)


def test_reused_old_runtime_is_never_rebuilt_or_pushed(candidates, monkeypatch):
    calls = []
    monkeypatch.setattr(cloud, "command", lambda *a: "")
    monkeypatch.setattr(images, "verify_registry_image", lambda *a: calls.append(a))
    monkeypatch.setattr(images, "registry_image", lambda *a: pytest.fail("Unexpected lookup"))
    monkeypatch.setattr(images, "publish_image", lambda *a, **k: pytest.fail("Unexpected copy"))
    cloud.promote(
        "project", "us-central1", SOURCE, SOURCE, PLATFORM, RUNTIME, SCIENCE, CONFIG, SCI_CONFIG
    )
    assert len(calls) == 2


def test_cloud_queue_and_step_pull_times_are_separate():
    result = latency.describe_timings(
        dict(
            id=BUILD,
            status="FAILURE",
            createTime="2026-10-19T00:00:00Z",
            startTime="2026-10-19T00:00:05Z",
            finishTime="2026-10-19T00:00:25Z",
            steps=[
                dict(
                    id="build",
                    status="FAILURE",
                    timing=dict(startTime="2026-10-19T00:00:10Z", endTime="2026-10-19T00:00:25Z"),
                    pullTiming=dict(
                        startTime="2026-10-19T00:00:05Z", endTime="2026-10-19T00:00:10Z"
                    ),
                )
            ],
        )
    )
    assert result["queue_seconds"] == 5
    assert result["execution_seconds"] == 20
    assert result["steps"][0]["seconds"] == 15
    assert result["steps"][0]["pull_seconds"] == 5


def test_failed_cloud_submission_still_records_latency(tmp_path, monkeypatch):
    monkeypatch.setattr(latency, "ROOT", tmp_path)

    def denied(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["gcloud"], stderr="PERMISSION_DENIED")

    monkeypatch.setattr(latency, "command", denied)
    with pytest.raises(subprocess.CalledProcessError):
        latency.submit("build", "project", "us-central1", SOURCE, SCIENCE, False)
    assert json.loads((tmp_path / "cloud-build.json").read_text())["status"] == "SUBMISSION_FAILED"


def test_publication_has_no_build_and_benchmark_skips_hosted_writes():
    root = Path(__file__).resolve().parents[2]
    config = yaml.safe_load((root / "cloudbuild.yaml").read_text())
    assert not any("docker build" in str(step) for step in config["steps"])
    for step in config["steps"]:
        if step["id"] in ("publish-catalogue", "publish", "verify-hosted"):
            assert "if test '${_PUBLISH_ONLY}' != true" in step["args"][-1]
    workflow = yaml.safe_load((root / ".github/workflows/platform.yml").read_text())
    stack = workflow["jobs"]["platform-stack"]["steps"]
    for step in stack:
        if step.get("id") == "cloud-build" or step.get("uses", "").startswith(
            "google-github-actions/"
        ):
            assert "github.event_name != 'pull_request'" in step["if"]
    assert workflow["jobs"]["latencies"]["if"] == "always()"
