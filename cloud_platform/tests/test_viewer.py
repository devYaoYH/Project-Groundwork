"""Existing research UI contracts use exclusively the authenticated corpus."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from cloud_platform.tests.test_api import platform, sign_in, create_experiment


def test_viewer_catalog_and_owned_evidence_contracts(platform):
    app, control, worker = platform
    alice = sign_in(app, "Alice")
    bob = sign_in(app, "Bob")
    experiment = create_experiment(alice)
    environments = alice.get("/api/environments").json()["environments"]
    assert next(row for row in environments if row["environment_id"] == "buyer_seller")["experiment_count"] == 1
    assert all(row["experiment_count"] == 0 for row in bob.get("/api/environments").json()["environments"])
    for suffix in ("", "/items", "/workflow"):
        response = alice.get("/api/environments/buyer_seller" + suffix)
        assert response.status_code == 200, response.text
    pool = alice.get("/api/agent-pool").json()
    assert pool["agents"] and all(row["credential_present"] is None for row in pool["agents"])
    validated = alice.post("/api/designs/validate", json={"release_id": experiment["release_id"], "design_text": experiment["design_text"]})
    assert validated.status_code == 200 and validated.json()["valid"]
    launched = alice.post("/api/launches", json={"experiment_id": experiment["id"], "mode": "smoke"})
    assert launched.status_code == 202, launched.text
    launch = launched.json()
    assert worker.run_once(launch["id"])
    detail = alice.get(f"/api/experiments/{experiment['id']}").json()
    assert detail["cells"] and detail["roster"] and detail["cell_evidence"]
    assert sum(row["completed_replicas"] for row in detail["cell_evidence"]) > 0
    assert detail["launches"][0]["launch"]["id"] == launch["id"]
    launch_detail = alice.get(f"/api/launches/{launch['id']}").json()
    assert launch_detail["id"] == launch_detail["launch"]["id"]
    assert launch_detail["progress"]["completed"] == launch["episode_count"]
    assert launch_detail["runner_logs"]
    assert alice.get(f"/api/launches/{launch['id']}/events").headers["content-type"].startswith("text/event-stream")
    page = alice.get(f"/api/episodes?experiment_id={experiment['id']}&limit=1").json()
    assert len(page["episodes"]) == 1 and page["facets"]["statuses"]
    assert bob.get("/api/episodes").json()["episodes"] == []
    uid = page["episodes"][0]["episode_uid"]
    trace = alice.get(f"/api/episodes/{uid}").json()
    assert trace["episode"]["events"] == trace["events"]
    assert trace["lanes"] and trace["cursor_max"] == len(trace["events"])
    assert alice.get(f"/api/episodes/{uid}/artifacts").json()["episode_uid"] == uid
    assert alice.get(f"/api/episodes/{uid}/observability").json()["episode_uid"] == uid
    assert alice.get(f"/api/episodes/{uid}/raw").json()["events"]
    for suffix in ("", "/artifacts", "/observability", "/raw"):
        assert bob.get(f"/api/episodes/{uid}" + suffix).status_code == 404
    stream = launch_detail["attempts"][0]["redis_stream"]
    events = alice.get(f"/api/streams/{stream}")
    assert events.status_code == 200 and events.json()["events"]
    assert alice.get(f"/api/streams/{stream}/trace").json()["events"]
    assert bob.get(f"/api/streams/{stream}").status_code == 404
    assert bob.get(f"/api/streams/{stream}/trace").status_code == 404
    assert bob.get(f"/api/launches/{launch['id']}/events").status_code == 404


def test_unknown_stream_names_and_filters_are_rejected(platform):
    app, _, _ = platform
    alice = sign_in(app, "Alice")
    assert alice.get("/api/streams/arbitrary-shared-redis-key").status_code == 404
    for query in ("limit=0", "cursor=-1", "experiment_id=a&experiment_id=b"):
        assert alice.get("/api/episodes?" + query).status_code == 400


def test_cell_evidence_uses_latest_attempt_including_failed_retry(platform):
    app, control, _ = platform
    alice = sign_in(app, "Alice")
    experiment = create_experiment(alice)
    configs = json.loads(control.artifacts.get(experiment["frozen_plan_key"]))
    config = configs[0]
    provenance = config["provenance"]
    common = {"owner_id": experiment["owner_id"], "experiment_id": experiment["id"],
              "cell_id": provenance["cell_id"], "episode_id": config["episode_id"]}
    for ordinal, score in ((1, 10), (2, 20)):
        control.repo.put("attempts", f"attempt-{ordinal}", {**common, "id": f"attempt-{ordinal}",
            "attempt": ordinal, "status": "COMPLETED", "launch_id": "private-test-launch"})
        control.repo.put("episodes", f"trace-{ordinal}", {**common, "id": f"trace-{ordinal}",
            "episode_uid": f"trace-{ordinal}", "attempt": ordinal, "status": "COMPLETED",
            "metrics": {"score": score}, "created_at": str(ordinal)})
    detail = alice.get(f"/api/experiments/{experiment['id']}").json()
    evidence = next(row for row in detail["cell_evidence"] if row["cell_id"] == provenance["cell_id"])
    assert evidence["completed_replicas"] == 1
    assert evidence["metric_summaries"] == [{"name": "score", "kind": "number", "n": 1,
                                           "mean": 20.0, "min": 20.0, "max": 20.0}]
    control.repo.put("attempts", "attempt-3", {**common, "id": "attempt-3", "attempt": 3,
        "status": "FAILED", "launch_id": "private-test-launch"})
    detail = alice.get(f"/api/experiments/{experiment['id']}").json()
    evidence = next(row for row in detail["cell_evidence"] if row["cell_id"] == provenance["cell_id"])
    assert evidence["completed_replicas"] == 0 and evidence["metric_summaries"] == []
    assert evidence["status_counts"]["FAILED"] == 1
    assert sum(evidence["status_counts"].values()) == evidence["planned_replicas"]
    # Historical physical observations remain available for review/export.
    assert len(alice.get("/api/episodes").json()["episodes"]) == 2


def test_existing_design_editor_mutations_remain_owned_and_digest_checked(platform):
    app, _, _ = platform
    alice = sign_in(app, "Alice")
    bob = sign_in(app, "Bob")
    source = create_experiment(alice)
    forked = alice.post(f"/api/experiments/{source['id']}/fork", json={})
    assert forked.status_code == 201, forked.text
    draft = forked.json()
    assert draft["status"] == "DRAFT" and draft["forked_from"] == source["id"]
    for suffix, payload in (("design", {"design_text": draft["design_text"]}),
                            ("lock", {"design_sha256": draft["design_sha256"]}), ("fork", {})):
        assert bob.post(f"/api/experiments/{draft['id']}/{suffix}", json=payload).status_code == 404
    text = draft["design_text"].replace("root: 41", "root: 42")
    saved = alice.post(f"/api/experiments/{draft['id']}/design", json={"design_text": text})
    assert saved.status_code == 200, saved.text
    updated = saved.json()
    assert updated["locked_at"] is None
    assert alice.post(f"/api/experiments/{draft['id']}/lock", json={"design_sha256": "outdated"}).status_code == 400
    locked = alice.post(f"/api/experiments/{draft['id']}/lock", json={"design_sha256": updated["design_sha256"]})
    assert locked.status_code == 200 and locked.json()["locked_at"]
    detail = alice.get(f"/api/experiments/{draft['id']}").json()
    assert detail["cells"] and detail["roster"]
    assert alice.post(f"/api/experiments/{draft['id']}/design", json={"design_text": text}).status_code == 400


def test_concurrent_save_wins_stale_lock_without_poisoning_later_lock(platform, monkeypatch):
    app, control, _ = platform
    alice = sign_in(app, "Alice")
    source = create_experiment(alice)
    draft = alice.post(f"/api/experiments/{source['id']}/fork", json={}).json()
    ready_to_commit = threading.Event()
    permit_commit = threading.Event()
    original_claim = control.repo.claim

    def pause_first_lock(kind, identifier, expected, patch, **kwargs):
        if kind == "experiments" and identifier == draft["id"] and patch.get("status") == "READY" and not ready_to_commit.is_set():
            ready_to_commit.set()
            assert permit_commit.wait(15), "save did not finish before the lock commit"
        return original_claim(kind, identifier, expected, patch, **kwargs)

    monkeypatch.setattr(control.repo, "claim", pause_first_lock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(control.lock_design, draft["id"], draft["owner_id"],
                              {"design_sha256": draft["design_sha256"]})
        try:
            assert ready_to_commit.wait(15), "lock did not finish compilation"
            orphan_keys = control.artifacts.keys(f"experiments/{draft['id']}/locked/")
            assert orphan_keys, "test must race after immutable artifacts were written"
            updated_text = draft["design_text"].replace("root: 41", "root: 42")
            saved = alice.post(f"/api/experiments/{draft['id']}/design", json={"design_text": updated_text})
            assert saved.status_code == 200, saved.text
        finally:
            permit_commit.set()
        with pytest.raises(ValueError, match="draft changed during locking"):
            pending.result(timeout=15)
    current = control.owned("experiments", draft["id"], draft["owner_id"])
    assert current["status"] == "DRAFT" and current["design_text"] == updated_text
    assert current["snapshot_key"] == saved.json()["snapshot_key"]
    locked = alice.post(f"/api/experiments/{draft['id']}/lock", json={"design_sha256": current["design_sha256"]})
    assert locked.status_code == 200, locked.text
    assert locked.json()["design_text"] == updated_text
    assert locked.json()["snapshot_key"] not in orphan_keys
    assert locked.json()["frozen_plan_key"] not in orphan_keys
    assert json.loads(control.artifacts.get(locked.json()["frozen_plan_key"]))


def test_original_ui_assets_do_not_publish_workspace_files(platform):
    from starlette.testclient import TestClient
    app, _, _ = platform
    client = TestClient(app)
    assert "research control plane" in client.get("/").text
    assert client.get("/environment-assets/negotiation/experiments/smoke_local.yaml").status_code == 404
    assert client.get("/environment-assets/calendar/tasks/../environment.yaml").status_code == 404
    assert client.get("/environment-replays/negotiation/").status_code == 200
    assert client.get("/environment-replays/negotiation/").headers["x-frame-options"] == "SAMEORIGIN"
    assert client.get("/environment-assets/negotiation/webapp/static/styles/base.css").status_code == 200


def test_haiku_55_vertex_choice_freezes_to_a_concrete_model(platform):
    """The Agents API and design compiler must agree on published bindings."""
    from pathlib import Path
    import yaml

    app, control, _ = platform
    alice = sign_in(app, "Alice")
    name = "haiku-5.5"
    response = alice.get("/api/agent-pool")
    assert response.status_code == 200
    entries = {entry["name"]: entry for entry in response.json()["agents"]}
    assert name in entries
    assert not {"sonnet-5.5", "opus-5.5"} & entries.keys()
    entry = entries[name]
    assert entry["model"] == "claude-haiku-5-5"
    assert entry["api_format"] == "vertexai_anthropic"
    assert entry["credential"] is None
    assert entry["credential_present"] is None

    workspace = Path(__file__).resolve().parents[2]
    design = yaml.safe_load((workspace / "local_stack/tests/buyer_seller_design_smoke.yaml").read_text())
    for participant in design["roster"]:
        participant.update(kind="llm", binding=name)
    releases = alice.get("/api/releases").json()["releases"]
    release = next(row for row in releases if row["environment_id"] == "buyer_seller")
    result = alice.post("/api/experiments", json={
        "name": name, "release_id": release["id"], "design_text": yaml.safe_dump(design),
    })
    assert result.status_code == 201, result.text
    experiment = result.json()
    assert experiment["locked_at"]
    plan = json.loads(control.artifacts.get(experiment["frozen_plan_key"]))
    assert plan
    for config in plan:
        assert [agent["model"] for agent in config["agents"]] == [entry["model"]] * 2
        assert all(agent["api_format"] == "vertexai_anthropic" for agent in config["agents"])
        assert all(agent["gcp_location"] == "global" for agent in config["agents"])
        assert all("api_key" not in agent and "credential" not in agent for agent in config["agents"])
