"""A smoke health check cannot replace a live measurement."""

import json
from pathlib import Path

from a2a_engine.manifest import EpisodeManifest
from a2a_engine.schemas import EpisodeConfigBase, EpisodeTrace
from a2a_engine.storage.schema import apply_schema
from a2a_engine.storage.results import RESULT_PREDICATE, counts_as_result
from a2a_engine.storage.sqlite import SQLiteEpisodeStore
from expt_runner.run_experiment import _make_run_context
from local_stack.control_plane import ControlPlane
from local_stack.server import LocalStackHandler
from local_stack.tests.fakes import FakeLauncher

WORKSPACE = Path(__file__).resolve().parents[2]
DESIGN = (Path(__file__).parent / "buyer_seller_design_smoke.yaml").read_text()


def _put(control, episode_id, attempt, mode, score):
    with control._session() as db:
        plans = db.execute("SELECT cell_id, episode_configs FROM cells").fetchall()
    config_data = next(config for row in plans for config in json.loads(row["episode_configs"])
                       if config["episode_id"] == episode_id)
    config_data["provenance"]["attempt"] = attempt
    config_data["provenance"]["run_mode"] = mode
    config = EpisodeConfigBase.model_validate(config_data)
    trace = EpisodeTrace(
        episode_uid=f"{mode}-{attempt}", config=config, metrics={"score": score},
    )
    manifest = EpisodeManifest.from_run(
        config=config.model_dump(), experiment_name="Mode isolation",
        cell_id=config_data["provenance"]["cell_id"], episode_idx=0, episode_uid=trace.episode_uid,
    )
    SQLiteEpisodeStore(path=control.path).put_episode(trace, manifest)


def test_live_result_survives_smoke_and_dry_run_and_two_step_agrees(tmp_path):
    control = ControlPlane(tmp_path / "a2a.db", workspace=WORKSPACE)
    experiment = control.create_experiment(
        name="Mode isolation", release_id="buyer_seller", design_text=DESIGN,
    )
    control.lock_experiment(experiment.id, design_sha256=experiment.design_sha256)
    locked_digest = control.experiment(experiment.id).design_sha256
    control.launcher = FakeLauncher()
    first = control.launch_experiment(experiment.id, mode="live")
    with control._session() as db:
        live_plan = json.loads(db.execute(
            "SELECT episode_configs FROM cells WHERE experiment_id = ? LIMIT 1",
            (experiment.id,),
        ).fetchone()[0])
    episode_id = control.planned_episode_ids(first.id)[0]
    _put(control, episode_id, 1, "live", 1.0)
    control.launcher.finish(first.id)
    control.reconcile()
    expected = control.cell_evidence(experiment.id)
    assert sum(cell["completed_replicas"] for cell in expected) == 1

    smoke = control.launch_experiment(experiment.id, mode="smoke")
    assert control.experiment(experiment.id).design_sha256 == locked_digest
    with control._session() as db:
        rows = db.execute("SELECT * FROM episodes").fetchall()
        assert all(counts_as_result(row) for row in rows)
        assert db.execute(f"SELECT COUNT(*) FROM episodes WHERE {RESULT_PREDICATE}").fetchone()[0] == len(rows)
    _put(control, episode_id, 2, "smoke", 0.0)
    control.launcher.finish(smoke.id)
    control.reconcile()
    assert control.launch_detail(smoke.id)["progress"]["completed"] == 1
    assert control.cell_evidence(experiment.id) == expected
    assert control._cell_evidence_two_step(experiment.id) == expected
    assert control._progress_rows_sql(smoke.id) == control._progress_rows_two_step(smoke.id)
    with control._session() as db:
        rows = db.execute("SELECT * FROM episodes").fetchall()
        assert sum(counts_as_result(row) for row in rows) == db.execute(
            f"SELECT COUNT(*) FROM episodes WHERE {RESULT_PREDICATE}"
        ).fetchone()[0] == 1
    assert all(config["provenance"].get("run_mode") is None for config in live_plan)
    previous = LocalStackHandler.database, LocalStackHandler._control_plane
    try:
        LocalStackHandler.database = control.path
        LocalStackHandler._control_plane = control
        page = LocalStackHandler._episode_page({"run_mode": ["smoke"]})
        assert [episode["run_mode"] for episode in page["episodes"]] == ["smoke"]
        assert {facet["value"] for facet in page["facets"]["run_modes"]} == {"live", "smoke"}
        assert LocalStackHandler._episode_detail("smoke-2")["run_mode"] == "smoke"
    finally:
        LocalStackHandler.database, LocalStackHandler._control_plane = previous

    dry = control.launch_experiment(experiment.id, mode="dry_run")
    control.launcher.finish(dry.id)
    control.reconcile()
    assert control.cell_evidence(experiment.id) == expected
    assert control._cell_evidence_two_step(experiment.id) == expected


def test_worker_overrides_plan_claim_without_mutating_design_identity():
    planned = {"cell_id": "cell", "episode_idx": 0, "episode_id": "exp.cell.000",
               "seed": 17, "attempt": 2,
               "provenance": {"design_sha256": "original", "attempt": 2, "run_mode": "live"}}
    cfg = {"environment_id": "buyer_seller", "_design_episode": planned}
    smoke = _make_run_context("exp", "cell", cfg, 0, dry_run=True, persist=True)
    dry = _make_run_context("exp", "cell", cfg, 0, dry_run=True, persist=False)
    assert smoke["config"]["provenance"]["run_mode"] == "smoke"
    assert dry["config"]["provenance"]["run_mode"] == "dry_run"
    assert smoke["config"]["provenance"]["design_sha256"] == "original"
    assert smoke["config"]["seed"] == 17
    assert planned["provenance"]["run_mode"] == "live"


def test_older_database_attributes_launches_and_cli_smoke_without_nulls(tmp_path):
    path = tmp_path / "old.db"
    store = SQLiteEpisodeStore(path=path)
    conn = store._connect()
    apply_schema(conn)
    conn.execute("INSERT INTO releases (id, environment_id) VALUES ('r', 'buyer_seller')")
    conn.execute("INSERT INTO experiments (id, name, environment_id, release_id, yaml_path, config_sha256, created_at) VALUES ('x','x','buyer_seller','r','','','now')")
    conn.execute("INSERT INTO launches (id, experiment_id, status, max_parallelism, trace_database, mode, created_at) VALUES ('s','x','COMPLETED',1,'', 'smoke','now')")
    conn.execute("INSERT INTO attempts (id, launch_id, episode_id, cell_id, episode_idx, attempt, status) VALUES ('a','s','design.cell.0','cell',0,1,'COMPLETED')")
    for uid, name, episode_id in (
        ("design-smoke", "design", "design.cell.0"),
        ("cli-smoke", "all_games_smoke", "cli.cell.0"),
        ("direct-live", "other", "direct.cell.0"),
    ):
        conn.execute(
            "INSERT INTO episodes (episode_uid, experiment_name, episode_id, config, events, final_state, metrics, manifest) VALUES (?,?,?,?,?,?,?,?)",
            (uid, name, episode_id, json.dumps({"provenance": {}}), "[]", "{}", "{}", "{}"),
        )
    conn.commit()
    conn.execute("DROP INDEX idx_episodes_run_mode")
    conn.execute("ALTER TABLE episodes DROP COLUMN run_mode")
    conn.commit()
    apply_schema(conn)
    rows = dict(conn.execute("SELECT episode_uid, run_mode FROM episodes").fetchall())
    assert rows == {"design-smoke": "smoke", "cli-smoke": "smoke", "direct-live": "live"}
    assert conn.execute("SELECT COUNT(*) FROM episodes WHERE run_mode IS NULL").fetchone()[0] == 0
    conn.close()
