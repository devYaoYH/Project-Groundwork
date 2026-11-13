"""Deployment preserves legacy pins and creates immutable infrastructure pairs."""
import copy
import hashlib
import io
import json
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from deploy import common, prepare_runtime, release, verify_hosted

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


@pytest.mark.parametrize("diagnostic", [
    "PERMISSION_DENIED",
    "ERROR: (gcloud.artifacts.docker.images.describe) Connection reset by peer",
    "PERMISSION_DENIED: Image not found.",
    "Network error while checking NOT_FOUND status",
    "ERROR: (gcloud.artifacts.docker.images.describe) Image not found. Access denied.",
])
def test_runtime_lookup_failure_aborts_instead_of_overwriting(tmp_path, monkeypatch, diagnostic):
    runtime_cli(tmp_path, monkeypatch)
    def denied(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["gcloud"], stderr=diagnostic)
    monkeypatch.setattr(prepare_runtime, "command", denied)
    with pytest.raises(RuntimeError, match="refusing to rebuild or overwrite"):
        prepare_runtime.main()
    assert not (tmp_path / "runtime-image").exists()


@pytest.mark.parametrize("diagnostic", [
    "NOT_FOUND: Requested entity was not found",
    "ERROR: (gcloud.artifacts.docker.images.describe) NOT_FOUND: Requested entity was not found",
    "ERROR: (gcloud.artifacts.docker.images.describe) Image not found.\n\n"
    "A valid container image can be referenced by tag or digest, has the format of\n"
    "  LOCATION-docker.DOMAIN/PROJECT-ID/REPOSITORY-ID/IMAGE:tag\n"
    "  LOCATION-docker.DOMAIN/PROJECT-ID/REPOSITORY-ID/IMAGE@sha256:digest\n",
])
def test_only_explicit_runtime_not_found_selects_build(tmp_path, monkeypatch, diagnostic):
    tag = runtime_cli(tmp_path, monkeypatch)
    def missing(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["gcloud"], stderr=diagnostic)
    monkeypatch.setattr(prepare_runtime, "command", missing)
    prepare_runtime.main()
    assert not (tmp_path / "runtime-existing").exists()
    assert (tmp_path / "runtime-image").read_text() == tag


def test_science_history_covers_copied_inputs_and_ci_tests_real_pair():
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "Dockerfile.runtime").read_text()
    assert "cloud_platform" not in dockerfile and "COPY deploy/" not in dockerfile and "COPY web/" not in dockerfile
    workflow = (root / ".github/workflows/platform.yml").read_text()
    for source in (".dockerignore", "Dockerfile.runtime", "scripts/install_workspace.py", "scripts/workspace.py", "a2a-engine", "a2a-agent", "a2a-judge", "expt-runner", "games", "experiments"):
        assert source in workflow.split("git log -1 --format=%H -- ")[1].split(')"')[0]
    assert "docker compose -f compose.platform.yml up --no-build --detach --wait" in workflow
    assert "deploy/verify_local.py --url http://localhost:8080" in workflow
    build = yaml.safe_load((root / "cloudbuild.yaml").read_text())
    assert "_RUNTIME_SOURCE_SHA" in build["substitutions"]
    assert next(step for step in build["steps"] if step["id"] == "validate-images")
    assert "Download the exact Cloud Build candidates" in workflow


@pytest.mark.parametrize("config_path", [".github/workflows/platform.yml", "deploy/cloudbuild-image.yaml"])
def test_runtime_smoke_uses_the_packaged_workspace_command(config_path):
    root = Path(__file__).resolve().parents[2]
    build = yaml.safe_load((root / config_path).read_text())
    if "jobs" in build:
        step = next(step for step in build["jobs"]["platform-stack"]["steps"]
                    if step.get("name") == "Test the release images without rebuilding")
        command = step["run"]
    else:
        step = next(step for step in build["steps"] if step["id"] == "runtime-tests")
        command = step["args"][-1]
    assert "python scripts/workspace.py smoke" in command
    assert "experiments/all_games_smoke.yaml" not in command
    assert "--max-parallelism 1" in command
    assert "--storage-path /tmp/science-smoke/a2a.db" in command
    assert "if ! test -e .platform-build/runtime-existing" in command
    # The build command must be executable in the narrow scientific image,
    # whose filesystem deliberately omits the deployment/control/web code.
    sources = {
        source
        for line in (root / "Dockerfile.runtime").read_text().splitlines()
        if line.startswith("COPY ")
        for source in line.split()[1:-1]
    }
    assert "scripts/workspace.py" in sources
    assert "scripts/install_workspace.py" in sources
    assert (root / "scripts/workspace.py").is_file()


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


def test_release_passes_published_catalogue_pin_to_revision(monkeypatch):
    pin = "c" * 64
    monkeypatch.setattr(sys, "argv", ["release", "--project", "project", "--image", WORKER_IMAGE,
        "--source-sha", NEW_SOURCE, "--runtime-image", NEW_IMAGE, "--runtime-source-sha", NEW_SOURCE,
        "--oauth-client-id", "test.apps.googleusercontent.com", "--catalogue-sha256", pin])
    monkeypatch.setattr(release, "exists", lambda args: False)
    monkeypatch.setattr(release, "service_url", lambda *args: "https://test.run.app")
    monkeypatch.setattr(release, "publish_catalog", lambda *args: None)
    calls = []
    def run(arguments, **kwargs):
        calls.append(arguments)
        if arguments[:3] == ["run", "jobs", "list"]:
            return "[]"
        return ""
    monkeypatch.setattr(release, "command", run)
    release.main()
    deployed = next(args for args in calls if args[:2] == ["run", "deploy"])
    variables = deployed[deployed.index("--set-env-vars") + 1]
    assert "PLATFORM_CATALOGUE_SHA256=" + pin in variables.split(",")
    monkeypatch.setattr(sys, "argv", sys.argv[:-1] + ["latest"])
    calls.clear()
    with pytest.raises(ValueError, match="published catalogue content hash"):
        release.main()
    assert not calls


def test_build_publishes_tested_image_catalogue_before_api_rollout():
    build = yaml.safe_load((Path(__file__).resolve().parents[2] / "cloudbuild.yaml").read_text())
    steps = build["steps"]
    order = {step["id"]: index for index, step in enumerate(steps)}
    assert order["validate-images"] < order["publish-catalogue"] < order["publish"] < order["verify-hosted"]
    assert not {"build", "build-runtime", "platform-tests", "push", "push-runtime"} & set(order)
    publication = steps[order["publish-catalogue"]]
    assert publication["name"] == "${_PLATFORM_IMAGE}"
    assert "--image '${_PLATFORM_IMAGE}'" in steps[order["publish"]]["args"][-1]
    assert "--runtime-image \"$$(cat .platform-build/promoted-runtime)\"" in steps[order["publish"]]["args"][-1]
    assert publication["entrypoint"] == "sh"
    assert "python -m cloud_platform.publish_catalogue" in publication["args"][-1]
    assert "--catalogue-sha256" in steps[order["publish"]]["args"][-1]
    assert ".platform-build/catalogue-sha256" in steps[order["publish"]]["args"][-1]


def catalog_upload_fixture():
    catalog = release.binding_catalog(WORKER_IMAGE, NEW_SOURCE,
        {NEW_DIGEST: {"runtime_image": NEW_IMAGE, "runtime_source_sha": NEW_SOURCE}})
    payload = json.dumps(catalog, sort_keys=True, separators=(",", ":")).encode()
    key = f"platform/runtime-bindings/{WORKER_DIGEST[7:]}/{hashlib.sha256(payload).hexdigest()}.json"
    return key, payload


def test_catalog_upload_uses_exact_object_create_precondition_and_private_token(monkeypatch, capsys):
    key, payload = catalog_upload_fixture()
    commands, requests = [], []
    def token(arguments, **kwargs):
        commands.append((arguments, kwargs))
        return "private-build-token"
    def upload(request, *, timeout):
        requests.append(request)
        assert timeout == 30
        return io.BytesIO(b'{"generation":"1"}')
    monkeypatch.setattr(release, "command", token)
    monkeypatch.setattr(release, "urlopen", upload)
    release.publish_catalog("project", key, payload)
    assert commands == [(["auth", "print-access-token", "--project", "project"], {"capture": True})]
    assert len(requests) == 1
    request = requests[0]
    url = urlsplit(request.full_url)
    assert url.scheme == "https" and url.netloc == "storage.googleapis.com"
    assert url.path == "/upload/storage/v1/b/project-groundwork-artifacts/o"
    assert parse_qs(url.query) == {"uploadType": ["media"], "name": [key], "ifGenerationMatch": ["0"]}
    assert request.get_method() == "POST" and request.data == payload
    assert request.get_header("Authorization") == "Bearer private-build-token"
    assert request.get_header("Content-type") == "application/json"
    assert "private-build-token" not in "".join(capsys.readouterr())


@pytest.mark.parametrize("conflicting", [False, True])
def test_existing_catalog_requires_exact_bytes_after_generation_precondition(monkeypatch, conflicting):
    key, payload = catalog_upload_fixture()
    requests = []
    monkeypatch.setattr(release, "command", lambda *args, **kwargs: "private-build-token")
    def upload(request, *, timeout):
        requests.append(request)
        assert timeout == 30
        if len(requests) == 1:
            raise HTTPError(request.full_url, 412, "Precondition Failed", {}, None)
        return io.BytesIO(payload + (b"changed" if conflicting else b""))
    monkeypatch.setattr(release, "urlopen", upload)
    if conflicting:
        with pytest.raises(RuntimeError, match="conflicting contents"):
            release.publish_catalog("project", key, payload)
    else:
        release.publish_catalog("project", key, payload)
    assert [request.get_method() for request in requests] == ["POST", "GET"]
    assert requests[1].full_url == (
        "https://storage.googleapis.com/storage/v1/b/project-groundwork-artifacts/o/"
        + key.replace("/", "%2F") + "?alt=media")
    assert all(request.get_header("Authorization") == "Bearer private-build-token" for request in requests)


@pytest.mark.parametrize("invalid", ["namespace", "content-hash", "worker-digest"])
def test_catalog_namespace_and_digest_validation_precedes_any_cloud_access(monkeypatch, invalid):
    key, payload = catalog_upload_fixture()
    if invalid == "namespace":
        key = key.replace("platform/runtime-bindings/", "users/alice/")
    elif invalid == "content-hash":
        payload += b" "
    else:
        key = key.replace(WORKER_DIGEST[7:], NEW_DIGEST[7:])
    def unexpected(*args, **kwargs):
        raise AssertionError("Invalid catalog attempted cloud access")
    monkeypatch.setattr(release, "command", unexpected)
    monkeypatch.setattr(release, "urlopen", unexpected)
    with pytest.raises(ValueError, match="private namespace"):
        release.publish_catalog("project", key, payload)


@pytest.mark.parametrize("failure", ["permission", "network"])
def test_catalog_permission_or_network_failure_prevents_api_rollout(monkeypatch, capsys, failure):
    calls = []
    monkeypatch.setattr(sys, "argv", ["release.py", "--project", "project", "--image", WORKER_IMAGE,
        "--source-sha", NEW_SOURCE, "--runtime-image", NEW_IMAGE, "--runtime-source-sha", NEW_SOURCE,
        "--oauth-client-id", "test.apps.googleusercontent.com", "--catalogue-sha256", "c" * 64])
    monkeypatch.setattr(release, "exists", lambda *args, **kwargs: False)
    def command(arguments, **kwargs):
        calls.append(arguments)
        if arguments[:3] == ["run", "jobs", "list"]:
            return "[]"
        if arguments[:2] == ["auth", "print-access-token"]:
            assert kwargs == {"capture": True}
            return "private-build-token"
        assert arguments[:3] == ["run", "jobs", "replace"]
        return 0
    def upload(request, *, timeout):
        if failure == "permission":
            raise HTTPError(request.full_url, 403, "Forbidden", {}, None)
        raise URLError("Connection reset by peer")
    monkeypatch.setattr(release, "command", command)
    monkeypatch.setattr(release, "urlopen", upload)
    with pytest.raises(RuntimeError, match="API rollout aborted"):
        release.main()
    assert not any(arguments[:3] == ["run", "deploy", "groundwork-control"] for arguments in calls)
    assert not any(arguments[0] == "storage" for arguments in calls)
    assert "private-build-token" not in "".join(capsys.readouterr())
