"""Progress is an identity join, and a restart resolves what it stranded.

The whole fan-out is materialised at launch time and ``episode_id`` is
deterministic, so "how far along is this launch" is a question the database can
answer without a live process, without a PID file, and without reading the
runner's stdout.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from a2a_engine.event_sink import open_event_sink
from a2a_engine.manifest import EpisodeManifest
from a2a_engine.schemas import EpisodeConfigBase, EpisodeTrace
from a2a_engine.storage.sqlite import SQLiteEpisodeStore
from a2a_engine.tracing import EventLog

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from local_stack.control_plane import ControlPlane

WORKSPACE = Path(__file__).resolve().parents[2]


def _control(tmp_path: Path) -> ControlPlane:
    return ControlPlane(tmp_path / "a2a.db", workspace=WORKSPACE)


def _experiment(control: ControlPlane):
    return control.create_experiment(
        release_id="buyer_seller",
        yaml_path="games/buyer-seller/experiments/example.yaml",
    )


def _record_episode(control: ControlPlane, episode_id: str, *, partial: bool = False) -> str:
    """Write an episode the way the runner would, into the same database."""
    config = EpisodeConfigBase(
        environment_id="buyer_seller", num_agents=2,
        experiment_name=episode_id.split(".")[0], episode_id=episode_id,
    )
    trace = EpisodeTrace(
        episode_uid=f"uid-{episode_id}", config=config,
        observability={"partial": True} if partial else {},
        stopped=partial,
    )
    manifest = EpisodeManifest.from_run(
        config=config.model_dump(), experiment_name=config.experiment_name or "",
        cell_id=episode_id.split(".")[1], episode_idx=0, episode_uid=trace.episode_uid,
    )
    manifest.episode_id = episode_id
    return SQLiteEpisodeStore(path=control.path).put_episode(trace, manifest)


class SilentLauncher:
    """Starts the launch and reports nothing at all.

    This is the case the stdout regex could not survive: no ``OK`` line ever
    arrives, so every attempt would have been written off as unreported.
    """

    def __init__(self, control: ControlPlane) -> None:
        self.control = control

    def launch(self, launch, _experiment, _on_line, *, smoke_test=False):
        self.control._mark_launch_started(launch.id)

    def cancel(self, _launch_id):
        return False


def test_progress_comes_from_the_identity_join_not_from_the_runners_stdout(tmp_path):
    control = _control(tmp_path)
    experiment = _experiment(control)
    control.launcher = SilentLauncher(control)
    launch = control.launch_experiment(experiment.id, smoke_test=True)

    assert control.progress(launch.id)["completed"] == 0

    planned = control.planned_episode_ids(launch.id)
    assert len(planned) == 4
    _record_episode(control, planned[0])
    _record_episode(control, planned[1])

    progress = control.progress(launch.id)
    assert progress["planned"] == 4
    assert progress["completed"] == 2
    assert {cell["cell_id"] for cell in progress["by_cell"]} == {
        "wide_surplus", "narrow_surplus", "no_gains_control", "monotonic",
    }
    assert sum(cell["planned"] for cell in progress["by_cell"]) == 4
    assert sum(cell["completed"] for cell in progress["by_cell"]) == 2


def test_a_recovered_partial_episode_does_not_count_as_progress(tmp_path):
    control = _control(tmp_path)
    experiment = _experiment(control)
    control.launcher = SilentLauncher(control)
    launch = control.launch_experiment(experiment.id, smoke_test=True)

    planned = control.planned_episode_ids(launch.id)
    _record_episode(control, planned[0], partial=True)

    assert control.progress(launch.id)["completed"] == 0


def test_a_restart_reconciles_a_stranded_running_launch(tmp_path):
    """A launcher persists no PID and its watcher thread dies with the
    process, so without this the launch reads RUNNING forever and cancelling
    it cannot help: there is no handle left to terminate."""
    control = _control(tmp_path)
    experiment = _experiment(control)
    control.launcher = SilentLauncher(control)
    launch = control.launch_experiment(experiment.id, smoke_test=True)
    assert control.launch(launch.id).status == "RUNNING"

    planned = control.planned_episode_ids(launch.id)
    for episode_id in planned:
        _record_episode(control, episode_id)

    # A new control plane against the same database is what a server restart
    # looks like from the database's point of view.
    restarted = ControlPlane(tmp_path / "a2a.db", workspace=WORKSPACE)

    assert restarted.launch(launch.id).status == "COMPLETED"
    detail = restarted.launch_detail(launch.id)
    assert {attempt["status"] for attempt in detail["attempts"]} == {"COMPLETED"}
    assert detail["progress"]["completed"] == 4
    # The episode uri is recovered by joining, not by parsing a log line.
    assert all(attempt["episode_uri"].startswith("sqlite:///") for attempt in detail["attempts"])


def test_a_restart_marks_episodes_that_never_ran_unreported(tmp_path):
    control = _control(tmp_path)
    experiment = _experiment(control)
    control.launcher = SilentLauncher(control)
    launch = control.launch_experiment(experiment.id, smoke_test=True)
    planned = control.planned_episode_ids(launch.id)
    _record_episode(control, planned[0])

    restarted = ControlPlane(tmp_path / "a2a.db", workspace=WORKSPACE)

    detail = restarted.launch_detail(launch.id)
    assert detail["launch"]["status"] == "FAILED"
    by_episode = {attempt["episode_id"]: attempt for attempt in detail["attempts"]}
    assert by_episode[planned[0]]["status"] == "COMPLETED"
    assert by_episode[planned[1]]["status"] == "UNREPORTED"
    # Recording it as completed would invent a result; the gap stays visible.
    assert by_episode[planned[1]]["episode_uri"] is None


def test_reconcile_recovers_a_partial_trace_from_the_event_log(tmp_path):
    """An attempt whose process died is resolved to a recoverable partial
    trace -- evidence for inspection and retry -- rather than to a bare gap."""
    control = _control(tmp_path)
    experiment = _experiment(control)
    control.launcher = SilentLauncher(control)
    launch = control.launch_experiment(experiment.id, smoke_test=True)
    episode_id = control.planned_episode_ids(launch.id)[0]

    sink = open_event_sink(
        control.results_dir, experiment_name=experiment.name,
        episode_uid="killed-1", episode_id=episode_id, environment_id="buyer_seller",
    )
    log = EventLog(sink=sink)
    log.append("game_start", {"num_agents": 2})
    log.append("offer", {"speaker": "seller", "price": 7})
    # No terminal event: this is where the process died.

    restarted = ControlPlane(tmp_path / "a2a.db", workspace=WORKSPACE)

    detail = restarted.launch_detail(launch.id)
    recovered = next(a for a in detail["attempts"] if a["episode_id"] == episode_id)
    assert recovered["status"] == "UNREPORTED"
    assert recovered["episode_uri"] and recovered["episode_uri"].endswith("#killed-1")
    assert "partial trace was recovered" in recovered["error"]

    trace = SQLiteEpisodeStore(path=tmp_path / "a2a.db").get_episode("killed-1")
    assert trace is not None
    assert trace.stopped is True
    assert trace.observability["partial"] is True
    assert [event.type for event in trace.events] == ["game_start", "offer"]
    # It is not a result, so it must not count as one.
    assert restarted.progress(launch.id)["completed"] == 0


def test_attempt_numbers_are_monotonic_per_episode_across_launches(tmp_path):
    control = _control(tmp_path)
    experiment = _experiment(control)
    control.launcher = SilentLauncher(control)

    first = control.launch_experiment(experiment.id, smoke_test=True)
    second = control.launch_experiment(experiment.id, smoke_test=True)

    def attempts(launch_id):
        return {
            a["episode_id"]: a["attempt"] for a in control.launch_detail(launch_id)["attempts"]
        }

    assert set(attempts(first.id).values()) == {1}
    assert set(attempts(second.id).values()) == {2}
    # A failed attempt is kept rather than tombstoned, so both rows survive.
    with sqlite3.connect(control.path) as db:
        count = db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    assert count == 8


def test_the_fact_table_joins_its_dimensions_in_one_file(tmp_path):
    """The join the two-file layout made impossible."""
    control = _control(tmp_path)
    experiment = _experiment(control)
    control.launcher = SilentLauncher(control)
    launch = control.launch_experiment(experiment.id, smoke_test=True)
    for episode_id in control.planned_episode_ids(launch.id):
        _record_episode(control, episode_id)

    with sqlite3.connect(control.path) as db:
        rows = db.execute(
            "SELECT a.cell_id, COUNT(e.episode_uid) FROM attempts a "
            "LEFT JOIN episodes e ON e.episode_id = a.episode_id "
            "WHERE a.launch_id = ? GROUP BY a.cell_id ORDER BY a.cell_id",
            (launch.id,),
        ).fetchall()
    assert rows == [("monotonic", 1), ("narrow_surplus", 1),
                    ("no_gains_control", 1), ("wide_surplus", 1)]
