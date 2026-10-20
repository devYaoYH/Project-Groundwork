import json
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml

from cloud_platform.tests.test_api import platform, sign_in, create_experiment
from cloud_platform.storage import Repository


def test_concurrent_retry_admission_creates_only_one_active_launch(tmp_path):
    repo = Repository(backend="local", data_dir=tmp_path)
    def admit(index):
        try:
            return repo.admit_launch({"id": str(index), "owner_id": "alice", "experiment_id": "study",
                                      "episode_count": 1, "retry_incomplete": True}, max_active=10)
        except ValueError as error:
            assert "active experiment" in str(error)
    with ThreadPoolExecutor(max_workers=4) as pool:
        admitted = list(pool.map(admit, range(4)))
    assert len([launch for launch in admitted if launch]) == 1
    assert repo.get("quotas", "alice")["active"] == 1


@pytest.mark.parametrize("one_episode", [False, True])
def test_owned_retry_runs_only_incomplete_frozen_episodes(platform, monkeypatch, one_episode):
    app, control, worker = platform
    alice, bob = sign_in(app, "Alice"), sign_in(app, "Bob")
    experiment = create_experiment(alice)
    body = {"experiment_id": experiment["id"], "mode": "smoke", "max_parallelism": 2}
    first = alice.post("/api/launches", json=body).json()
    attempts = control.repo.list("attempts", owner_id="local:alice", experiment_id=experiment["id"])
    assert len(attempts) >= 2
    # Simulate a launch ending after one completed replication, with the rest failed.
    control.repo.claim("launches", first["id"], {"QUEUED"}, {"status": "PARTIAL", "evidence_complete": True})
    for index, attempt in enumerate(attempts):
        control.repo.put("attempts", attempt["id"], {**attempt, "status": "COMPLETED" if index == 0 else "FAILED"})
    options = alice.get(f"/api/experiments/{experiment['id']}").json()["retry_options"]
    incomplete = sorted(a["episode_id"] for a in attempts[1:])
    assert options == [{"mode": "smoke", "episode_ids": incomplete, "max_parallelism": 2}]
    retry_body = {**body, "retry_incomplete": True}
    if one_episode:
        retry_body["episode_ids"] = incomplete[:1]
    assert bob.post("/api/launches", json=retry_body).status_code == 404
    for ids in ([attempts[0]["episode_id"]], ["foreign-episode"], []):
        assert alice.post("/api/launches", json={**retry_body, "episode_ids": ids}).status_code == 400
    # Retry must not consult the current compiler/catalog to reconstruct the design.
    monkeypatch.setattr(control, "compiler", lambda *a, **kw: pytest.fail("recompiled a frozen experiment"))
    response = alice.post("/api/launches", json=retry_body)
    assert response.status_code == 202, response.text
    retry = response.json()
    assert retry["runtime_image_digest"] == first["runtime_image_digest"]
    assert retry["runtime_source_sha"] == first["runtime_source_sha"]
    expected = incomplete[:1] if one_episode else incomplete
    plan = yaml.safe_load(control.artifacts.get(retry["plan_key"]))
    configs = [cell["config"] for cell in plan["cells"]]
    assert sorted(config["episode_id"] for config in configs) == expected
    frozen = {c["episode_id"]: c for c in json.loads(control.artifacts.get(experiment["frozen_plan_key"]))}
    for config in configs:
        assert config["seed"] == frozen[config["episode_id"]]["seed"]
        assert config["provenance"]["attempt"] == 2
    assert alice.post("/api/launches", json=retry_body).status_code == 400
    assert alice.get(f"/api/experiments/{experiment['id']}").json()["retry_options"] == []
    assert worker.run_once(retry["id"])
    finished = alice.get(f"/api/launches/{retry['id']}").json()
    assert finished["evidence_complete"] and finished["status"] == "COMPLETED", finished
    assert sorted(a["episode_id"] for a in finished["attempts"]) == expected
    assert all(a["attempt"] == 2 for a in finished["attempts"])
    assert alice.get(f"/api/launches/{first['id']}").json()["status"] == "PARTIAL"
    options = alice.get(f"/api/experiments/{experiment['id']}").json()["retry_options"]
    remaining = sorted(set(incomplete) - set(expected))
    assert options == ([{"mode": "smoke", "episode_ids": remaining, "max_parallelism": 2}] if remaining else [])
