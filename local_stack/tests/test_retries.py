import pytest
import yaml

from local_stack.tests.test_experiments import SilentLauncher, control, create
from local_stack.retries import retry_options, select_retry_configs


def test_design_retry_preserves_plan_and_attempt_history(tmp_path):
    plane = control(tmp_path)
    plane.launcher = SilentLauncher(plane)
    experiment = create(plane)
    plane.lock_experiment(experiment.id, design_sha256=experiment.design_sha256)
    original = plane.launch_experiment(experiment.id, mode="smoke")
    attempts = plane.launch_detail(original.id)["attempts"]
    complete, failed = attempts
    with plane._session() as db:
        db.execute("UPDATE launches SET status='PARTIAL' WHERE id=?", (original.id,))
        db.execute("UPDATE attempts SET status='COMPLETED' WHERE id=?", (complete["id"],))
        db.execute("UPDATE attempts SET status='FAILED' WHERE id=?", (failed["id"],))
    assert plane.experiment_detail(experiment.id)["retry_options"][0]["episode_ids"] == [failed["episode_id"]]
    with pytest.raises(ValueError, match="cannot be retried"):
        plane.launch_experiment(experiment.id, mode="smoke", retry_incomplete=True,
                                episode_ids=[complete["episode_id"]])
    frozen = plane._design_episode_configs(plane.experiment(experiment.id), mode="smoke")
    retry = plane.launch_experiment(experiment.id, mode="smoke", retry_incomplete=True)
    retried = plane.launch_detail(retry.id)["attempts"]
    assert [(a["episode_id"], a["attempt"]) for a in retried] == [(failed["episode_id"], 2)]
    from pathlib import Path
    config = yaml.safe_load(Path(retry.execution_path).read_text())["cells"][0]["config"]
    expected = next(c for c in frozen if c["episode_id"] == failed["episode_id"])
    assert config["seed"] == expected["seed"]
    assert config["agents"] == expected["agents"]
    assert config["provenance"]["attempt"] == 2
    assert plane.launch_detail(original.id)["attempts"] == [
        {**a, "status": "COMPLETED" if a["id"] == complete["id"] else "FAILED"} for a in attempts]
    assert plane.experiment_detail(experiment.id)["retry_options"] == []
    with pytest.raises(ValueError, match="active"):
        plane.launch_experiment(experiment.id, mode="smoke", retry_incomplete=True)
    event = next(e for e in plane.events(retry.id) if e["kind"] == "launch.queued")
    assert event["payload"]["retry_incomplete"] is True
    assert event["payload"]["episode_ids"] == [failed["episode_id"]]


def test_retry_selection_uses_latest_attempt_in_each_mode_and_requires_final_evidence():
    launches = [dict(id="live", mode="live", status="PARTIAL", max_parallelism=2),
                dict(id="smoke", mode="smoke", status="COMPLETED", max_parallelism=1),
                dict(id="retry", mode="live", status="FAILED", max_parallelism=2, evidence_complete=False)]
    attempts = [dict(launch_id="live", episode_id="a", attempt=1, status="FAILED"),
                dict(launch_id="smoke", episode_id="a", attempt=2, status="COMPLETED"),
                dict(launch_id="retry", episode_id="b", attempt=3, status="COMPLETED")]
    assert retry_options(launches, attempts) == [{"mode": "live", "episode_ids": ["a", "b"], "max_parallelism": 2}]
    launches[-1]["evidence_complete"] = True
    attempts.append(dict(launch_id="retry", episode_id="a", attempt=3, status="COMPLETED"))
    assert retry_options(launches, attempts) == []
    with pytest.raises(ValueError, match="cannot be retried"):
        select_retry_configs([], launches, attempts, mode="live", episode_ids=["a"])


@pytest.mark.parametrize("ids", [[], "episode", [None], [""]])
def test_retry_rejects_invalid_selection(ids):
    with pytest.raises(ValueError, match="non-empty list"):
        select_retry_configs([], [], [], mode="smoke", episode_ids=ids)
