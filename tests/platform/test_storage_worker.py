import json
import sqlite3
from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import pytest

from cloud_platform.storage import ArtifactStore, Repository
from cloud_platform.worker import Worker

WORKSPACE = Path(__file__).resolve().parents[2]
DESIGN = """schema_version: 1
release: buyer_seller@v1
parameters:
  seller_cost: {randomize: true}
  buyer_value: {pin: 30}
  num_items: {pin: 3}
  discount_factor: {pin: 0.5}
units:
  episodes_per_cell: 1
roster:
  - {id: seller, role: seller, kind: scripted, binding: seller-baseline}
  - {id: buyer, role: buyer, kind: scripted, binding: buyer-baseline}
seed:
  root: 41
"""


def test_repository_scoped_lists_and_atomic_claim(tmp_path):
    repo = Repository(data_dir=tmp_path)
    repo.put("experiments", "a", {"id": "a", "owner_id": "alice", "experiment_id": "exp"})
    repo.put("experiments", "b", {"id": "b", "owner_id": "bob", "experiment_id": "exp"})
    assert [r["id"] for r in repo.list("experiments", owner_id="alice")] == ["a"]
    assert repo.create("launches", "l", {"status": "QUEUED"})
    assert not repo.create("launches", "l", {"status": "FAILED"})
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: repo.claim("launches", "l", {"QUEUED"}, {"status": "RUNNING"}), range(8)))
    assert sum(c is not None for c in claims) == 1


def test_atomic_quota_admission_release_and_no_double_release(tmp_path):
    repo = Repository(data_dir=tmp_path)
    def admit(index):
        try:
            return repo.admit_launch({"id": str(index), "owner_id": "alice", "episode_count": 2})
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=6) as pool:
        admitted = [r for r in pool.map(admit, range(6)) if r]
    assert len(admitted) == 2
    first = admitted[0]["id"]
    repo.claim("launches", first, {"QUEUED"}, {"status": "FAILED"})
    assert repo.get("quotas", "alice")["active"] == 1
    assert repo.claim("launches", first, {"QUEUED"}, {"status": "FAILED"}) is None
    repo.admit_launch({"id": "next", "owner_id": "alice", "episode_count": 2})
    with pytest.raises(ValueError, match="100 episodes"):
        repo.admit_launch({"id": "too-many", "owner_id": "bob", "episode_count": 101})


def test_repeated_attempt_ordinals_are_unique_under_concurrency(tmp_path):
    repo = Repository(data_dir=tmp_path)
    repo.create("experiments", "exp", {"id": "exp", "owner_id": "alice"})
    with ThreadPoolExecutor(max_workers=8) as pool:
        ordinals = list(pool.map(lambda _: repo.next_attempt("exp"), range(8)))
    assert sorted(ordinals) == list(range(1, 9))
    assert repo.next_attempt("exp") == 9


def test_stale_reconciliation_releases_capacity_and_preserves_evidence(tmp_path):
    repo = Repository(data_dir=tmp_path)
    artifacts = ArtifactStore(data_dir=tmp_path)
    current = datetime.now(UTC)
    repo.admit_launch({"id": "dead", "owner_id": "alice", "experiment_id": "exp"})
    repo.claim("launches", "dead", {"QUEUED"}, {"status": "RUNNING", "heartbeat_at": (current - timedelta(seconds=181)).isoformat()})
    repo.create("attempts", "a", {"id": "a", "launch_id": "dead", "owner_id": "alice", "experiment_id": "exp", "status": "RUNNING"})
    artifacts.put("launches/dead/events/uid/00000000.json", b"{\"type\":\"message\"}")
    repo.admit_launch({"id": "healthy", "owner_id": "bob", "experiment_id": "other"})
    repo.claim("launches", "healthy", {"QUEUED"}, {"status": "RUNNING", "heartbeat_at": current.isoformat()})
    assert repo.reconcile_stale(current, owner_id="bob") == []
    interrupted = repo.reconcile_stale(current, owner_id="alice")
    assert [r["id"] for r in interrupted] == ["dead"]
    assert interrupted[0]["status"] == "INTERRUPTED"
    assert repo.get("attempts", "a")["status"] == "INTERRUPTED"
    assert repo.get("quotas", "alice")["active"] == 0
    assert repo.get("launches", "healthy")["status"] == "RUNNING"
    assert artifacts.get("launches/dead/events/uid/00000000.json")
    assert repo.reconcile_stale(current, owner_id="alice") == []


def test_reconciliation_does_not_override_concurrent_heartbeat(tmp_path, monkeypatch):
    repo = Repository(data_dir=tmp_path)
    current = datetime.now(UTC)
    repo.admit_launch({"id": "alive", "owner_id": "alice", "experiment_id": "exp"})
    old = (current - timedelta(seconds=200)).isoformat()
    repo.claim("launches", "alive", {"QUEUED"}, {"status": "RUNNING", "heartbeat_at": old})
    original = repo.claim
    def racing_claim(kind, identifier, statuses, patch, **kwargs):
        original("launches", "alive", {"RUNNING"}, {"heartbeat_at": current.isoformat()})
        return original(kind, identifier, statuses, patch, **kwargs)
    monkeypatch.setattr(repo, "claim", racing_claim)
    assert repo.reconcile_stale(current) == []
    assert repo.get("launches", "alive")["status"] == "RUNNING"
    assert repo.get("quotas", "alice")["active"] == 1


def test_artifacts_immutable_and_path_safe(tmp_path):
    artifacts = ArtifactStore(data_dir=tmp_path)
    artifacts.put("launches/a/file.json", b"{}")
    artifacts.put("launches/a/file.json", b"{}")
    with pytest.raises(ValueError, match="conflict"):
        artifacts.put("launches/a/file.json", b"different")
    assert artifacts.get("launches/a/file.json") == b"{}"
    assert artifacts.keys("launches/a/") == ["launches/a/file.json"]
    for key in ("../escape", "/escape", "a/../../escape", "\\escape"):
        with pytest.raises(ValueError):
            artifacts.put(key, b"x")


def test_worker_scripted_scientific_identity_and_duplicate_delivery(tmp_path):
    from local_stack.control_plane import ControlPlane
    class Noop:
        def launch(self, *_args, **_kwargs): pass
    control = ControlPlane(tmp_path / "compile.sqlite", workspace=WORKSPACE)
    control.seed_installed_releases()
    control.launcher = Noop()
    experiment = control.create_experiment(name="worker-test", release_id="buyer_seller", design_text=DESIGN)
    control.lock_experiment(experiment.id, design_sha256=experiment.design_sha256)
    launch = control.launch_experiment(experiment.id, mode="smoke")
    artifacts = ArtifactStore(data_dir=tmp_path / "store")
    repository = Repository(data_dir=tmp_path / "store")
    snapshot = tmp_path / "snapshot.sqlite"
    with sqlite3.connect(control.path) as source, sqlite3.connect(snapshot) as destination:
        source.backup(destination)
    artifacts.put("input/snapshot.sqlite", snapshot.read_bytes())
    artifacts.put("input/plan.yaml", Path(launch.execution_path).read_bytes())
    repository.admit_launch({**asdict(launch), "owner_id": "alice", "snapshot_key": "input/snapshot.sqlite", "plan_key": "input/plan.yaml", "episode_count": 1})
    worker = Worker(repository, artifacts, workspace=WORKSPACE)
    assert worker.run_once(launch.id)
    result = repository.get("launches", launch.id)
    assert result["status"] == "COMPLETED", artifacts.get(f"launches/{launch.id}/runner.log").decode()
    assert result["evidence_complete"]
    assert not worker.run_once(launch.id)
    episodes = repository.list("episodes", owner_id="alice", experiment_id=experiment.id)
    assert len(episodes) == 1
    assert "events" not in episodes[0]
    row = json.loads(artifacts.get(episodes[0]["artifact_key"]))
    assert row["episode_id"] == control.planned_episode_ids(launch.id)[0]
    assert row["events"] and row["manifest"]
    assert len(repository.list("attempts", owner_id="alice")) == 1
    assert repository.get("quotas", "alice")["active"] == 0


def test_worker_download_failure_fails_without_second_execution(tmp_path):
    repository = Repository(data_dir=tmp_path)
    artifacts = ArtifactStore(data_dir=tmp_path)
    repository.admit_launch({"id": "broken", "owner_id": "alice", "snapshot_key": "missing", "plan_key": "missing", "experiment_id": "x"})
    worker = Worker(repository, artifacts, workspace=WORKSPACE)
    with pytest.raises(FileNotFoundError):
        worker.run_once("broken")
    assert repository.get("launches", "broken")["status"] == "FAILED"
    assert not worker.run_once("broken")
    assert repository.get("quotas", "alice")["active"] == 0


def test_remote_event_sink_fails_closed(monkeypatch, tmp_path):
    from a2a_engine.event_sink import JsonlEventSink
    from a2a_engine.schemas import Event
    monkeypatch.setenv("A2A_DURABLE_EVENT_SINK", "cloud_platform.storage:durable_event_write")
    monkeypatch.setenv("A2A_DURABLE_EVENT_PREFIX", "launches/test/events")
    import cloud_platform.storage as storage
    class FailingStore:
        def put(self, *_): raise RuntimeError("remote unavailable")
    monkeypatch.setattr(storage, "_event_store", FailingStore())
    sink = JsonlEventSink(tmp_path / "events.jsonl", episode_uid="uid")
    with pytest.raises(RuntimeError, match="remote unavailable"):
        sink.write(Event(type="message"))
    sink.close()
    assert (tmp_path / "events.jsonl").read_text()
