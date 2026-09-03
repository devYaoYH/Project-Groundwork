"""Design records are locked, forked, and launched as fixed episode plans."""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from local_stack.control_plane import ControlPlane, DesignDigestMismatch


WORKSPACE = Path(__file__).resolve().parents[2]

DESIGN = """schema_version: 1
release: buyer_seller@v1
parameters:
  seller_cost: {randomize: true}
  buyer_value: {pin: 30}
  num_items: {pin: 3}
  discount_factor: {factor: [0.5, 1.0]}
units:
  episodes_per_cell: 1
roster:
  - id: seller
    kind: scripted
    binding: seller-baseline
  - id: buyer
    kind: scripted
    binding: buyer-baseline
seed:
  root: 41
"""


class SilentLauncher:
    def __init__(self, control: ControlPlane) -> None:
        self.control = control

    def launch(self, launch, _experiment, _on_line, *, smoke_test=False):
        self.control._mark_launch_started(launch.id)

    def cancel(self, _launch_id):
        return False


def control(tmp_path: Path) -> ControlPlane:
    return ControlPlane(tmp_path / "a2a.db", workspace=WORKSPACE)


def create(plane: ControlPlane):
    return plane.create_experiment(
        name="Buyer seller pilot", release_id="buyer_seller", design_text=DESIGN
    )


def test_lock_stores_verbatim_text_and_full_fixed_plan(tmp_path):
    plane = control(tmp_path)
    experiment = create(plane)

    locked = plane.lock_experiment(experiment.id, design_sha256=experiment.design_sha256 or "")

    assert locked.locked_at is not None
    assert locked.design_text == DESIGN
    detail = plane.experiment_detail(locked.id)
    assert len(detail["cells"]) == 2
    assert all(cell["episodes_planned"] == 1 for cell in detail["cells"])
    with sqlite3.connect(plane.path) as db:
        config = db.execute(
            "SELECT episode_configs FROM cells WHERE experiment_id = ?", (locked.id,)
        ).fetchone()[0]
    assert '"design_sha256"' in config
    assert '"item_attributes"' in config


def test_locked_design_requires_an_explicit_fork_before_editing(tmp_path):
    plane = control(tmp_path)
    experiment = create(plane)
    locked = plane.lock_experiment(experiment.id, design_sha256=experiment.design_sha256 or "")

    with pytest.raises(ValueError, match="fork it before editing"):
        plane.update_experiment_design(
            locked.id,
            design_text=DESIGN.replace(
                "seller_cost: {randomize: true}", "seller_cost: {pin: 8}"
            ),
        )

    fork = plane.fork_experiment_design(locked.id)

    assert fork.id != locked.id
    assert fork.forked_from == locked.id
    assert fork.locked_at is None
    assert fork.design_text == DESIGN
    assert plane.experiment(locked.id).design_text == DESIGN


def test_live_launch_requires_a_lock_but_smoke_and_dry_run_are_allowed(tmp_path):
    plane = control(tmp_path)
    experiment = create(plane)
    plane.launcher = SilentLauncher(plane)

    with pytest.raises(ValueError, match="locked preregistration"):
        plane.launch_experiment(experiment.id, mode="live")

    smoke = plane.launch_experiment(experiment.id, mode="smoke")
    dry_run = plane.launch_experiment(experiment.id, mode="dry_run")

    assert smoke.mode == "smoke"
    assert dry_run.mode == "dry_run"
    assert smoke.execution_path is not None
    assert Path(smoke.execution_path).is_file()


def test_launch_refuses_design_text_that_no_longer_matches_the_locked_digest(tmp_path):
    plane = control(tmp_path)
    experiment = create(plane)
    locked = plane.lock_experiment(experiment.id, design_sha256=experiment.design_sha256 or "")
    with sqlite3.connect(plane.path) as db:
        db.execute(
            "UPDATE experiments SET design_text = ? WHERE id = ?",
            (DESIGN.replace("1.0", "0.9"), locked.id),
        )

    with pytest.raises(DesignDigestMismatch):
        plane.launch_experiment(locked.id)


def test_locked_launch_writes_the_runner_config_with_planned_provenance(tmp_path):
    plane = control(tmp_path)
    experiment = create(plane)
    locked = plane.lock_experiment(experiment.id, design_sha256=experiment.design_sha256 or "")
    plane.launcher = SilentLauncher(plane)

    launch = plane.launch_experiment(locked.id, mode="smoke")
    plan = Path(launch.execution_path or "").read_text(encoding="utf-8")

    assert "_design_episode:" in plan
    assert "experiment_id:" in plan
    assert "item_attributes:" in plan
    assert plane.planned_episode_ids(launch.id)


def test_locked_smoke_launch_persists_compiled_provenance(tmp_path):
    plane = control(tmp_path)
    experiment = create(plane)
    locked = plane.lock_experiment(experiment.id, design_sha256=experiment.design_sha256 or "")

    launch = plane.launch_experiment(locked.id, mode="smoke")
    for _ in range(100):
        current = plane.launch(launch.id)
        if current.status in {"COMPLETED", "FAILED", "CANCELLED"}:
            break
        time.sleep(0.05)

    assert plane.launch(launch.id).status == "COMPLETED"
    detail = plane.launch_detail(launch.id)
    assert detail["progress"]["completed"] == 2
    episode_uid = detail["attempts"][0]["episode_uri"].split("#", 1)[1]
    trace = plane._store().get_episode(episode_uid)
    assert trace is not None
    provenance = trace.config.model_dump()["provenance"]
    assert provenance["experiment_id"] == locked.id
    assert provenance["design_sha256"] == locked.design_sha256
    assert set(provenance["item_attributes"]) == {"seller_cost"}
