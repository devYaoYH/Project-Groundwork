import copy
import json

import pytest

from cloud_platform.runtime_bindings import resolve_binding, validate_catalog

RUNTIME = "sha256:" + "1" * 64
WORKER = "sha256:" + "a" * 64
SOURCE = "1" * 40
WORKER_SOURCE = "a" * 40


@pytest.fixture
def catalog():
    return {"schema_version": 1,
            "worker": {"image_digest": WORKER, "source_sha": WORKER_SOURCE,
                       "image": "registry/platform@" + WORKER},
            "runtimes": {RUNTIME: {"runtime_image": "registry/legacy-platform@" + RUNTIME,
                "runtime_source_sha": SOURCE, "job_name": "groundwork-run-old-new", "contract_version": 1}}}


def test_legacy_experiment_uses_registered_runtime_and_current_worker(catalog):
    validate_catalog(catalog, worker_digest=WORKER, worker_source_sha=WORKER_SOURCE)
    experiment = {"source_sha": SOURCE, "image_digest": RUNTIME,
                  "job_name": "old-frozen-job", "live_logs": False}
    previous = copy.deepcopy(experiment)
    binding = resolve_binding(catalog, experiment)
    assert binding["job_name"] == "groundwork-run-old-new"
    assert binding["runtime_image_digest"] == RUNTIME
    assert binding["worker_image_digest"] == WORKER
    assert binding["live_logs"] is True
    assert experiment == previous


@pytest.mark.parametrize("mutation", [
    lambda c: c.update(schema_version=2),
    lambda c: c["worker"].update(image_digest="sha256:" + "b" * 64),
    lambda c: c["worker"].update(image="registry/platform:latest"),
    lambda c: c["runtimes"][RUNTIME].update(runtime_image="registry/runtime:latest"),
    lambda c: c["runtimes"][RUNTIME].update(runtime_source_sha="unknown"),
    lambda c: c["runtimes"][RUNTIME].update(contract_version=2),
    lambda c: c["runtimes"][RUNTIME].update(job_name="../unrelated-job"),
])
def test_rejects_invalid_or_wrong_worker_catalog(catalog, mutation):
    mutation(catalog)
    with pytest.raises(ValueError):
        validate_catalog(catalog, worker_digest=WORKER, worker_source_sha=WORKER_SOURCE)


def test_never_substitutes_current_runtime_for_missing_pin(catalog):
    with pytest.raises(ValueError, match="unavailable"):
        resolve_binding(catalog, {"image_digest": "sha256:" + "2" * 64, "source_sha": SOURCE})
    with pytest.raises(ValueError, match="source"):
        resolve_binding(catalog, {"image_digest": RUNTIME, "source_sha": "2" * 40})


def test_unavailable_binding_fails_before_attempt_or_quota_admission(tmp_path):
    from cloud_platform.control import PlatformControl
    from cloud_platform.storage import ArtifactStore, Repository
    repository = Repository(data_dir=tmp_path)
    control = PlatformControl(repository, ArtifactStore(data_dir=tmp_path))
    repository.create("experiments", "old", {"id": "old", "owner_id": "alice", "status": "READY",
        "source_sha": SOURCE, "image_digest": RUNTIME, "launch_ordinal": 0})
    with pytest.raises(ValueError, match="no longer matches"):
        control.launch("old", "alice", {"mode": "smoke"})
    assert repository.get("experiments", "old")["launch_ordinal"] == 0
    assert repository.list("launches") == []
    assert repository.list("quotas") == []


@pytest.mark.parametrize("contract", [None, 1])
def test_dispatch_names_worker_only_for_paired_jobs(monkeypatch, contract):
    from cloud_platform.api import dispatch
    from types import SimpleNamespace
    sent = []
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    monkeypatch.setenv("PLATFORM_REGION", "us-central1")
    monkeypatch.setattr("google.auth.default", lambda **_: (object(), "test-project"))
    class Session:
        def __init__(self, *_): pass
        def post(self, url, **kwargs):
            sent.append(kwargs["json"])
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"name": "execution-operation"})
    monkeypatch.setattr("google.auth.transport.requests.AuthorizedSession", Session)
    class Repo:
        def get(self, *_): return {"id": "launch", "status": "QUEUED", "job_name": "groundwork-run-old-new",
                                 "worker_contract_version": contract}
        def claim(self, *_): pass
    dispatch(SimpleNamespace(repo=Repo()), "launch")
    override = {"env": [{"name": "PLATFORM_LAUNCH_ID", "value": "launch"}]}
    if contract:
        override["name"] = "worker"
    assert sent == [{"overrides": {"containerOverrides": [override]}}]
