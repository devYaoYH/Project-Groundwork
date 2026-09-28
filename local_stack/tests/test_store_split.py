"""The viewer resolves its episode store by name, and launch truth is portable.

Two things had co-location baked into them. The viewer imported
``SQLiteEpisodeStore`` directly, so no other backend could ever serve it; and
the two queries that define whether a launch succeeded were single SQL
statements joining ``attempts`` against ``episodes``, which is only expressible
while the two tables share a file.

Both are now written twice: resolution goes through ``make_store`` behind the
wider ``ControlPlaneReader`` protocol, and each join has a Python two-step
reference beside the single-statement optimization. These tests are what keep
the two implementations honest about being the same answer.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from a2a_engine.manifest import EpisodeManifest
from a2a_engine.schemas import EpisodeConfigBase, EpisodeTrace
from a2a_engine.storage import ControlPlaneReader, make_control_plane_reader
from a2a_engine.storage.sqlite import SQLiteEpisodeStore

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from local_stack.control_plane import ControlPlane
from local_stack.launchers.local_process import WORKER_RESULTS_DIRNAME
from local_stack.server import LocalStackHandler
from local_stack.tests.fakes import FakeLauncher

WORKSPACE = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _restore_handler_state():
    """``LocalStackHandler`` holds its configuration on the class.

    Pointing it at a temporary database is how these tests reach the real
    handler code; putting it back is how they avoid deciding what the next
    test reads.
    """
    saved = (
        LocalStackHandler.database,
        LocalStackHandler.workspace,
        LocalStackHandler._control_plane,
    )
    yield
    (LocalStackHandler.database, LocalStackHandler.workspace,
     LocalStackHandler._control_plane) = saved


def _control(tmp_path: Path) -> ControlPlane:
    return ControlPlane(tmp_path / "a2a.db", workspace=WORKSPACE)


def _experiment(control: ControlPlane):
    return control.create_experiment(
        release_id="buyer_seller",
        yaml_path="games/buyer-seller/experiments/example.yaml",
    )


def _record_episode(control: ControlPlane, episode_id: str, *, attempt: int = 1,
                    partial: bool = False, uid: str | None = None) -> str:
    """Write an episode the way a worker would, into the episode store."""
    config = EpisodeConfigBase(
        environment_id="buyer_seller", num_agents=2,
        experiment_name=episode_id.split(".")[0], episode_id=episode_id,
        provenance={"attempt": attempt},
    )
    trace = EpisodeTrace(
        episode_uid=uid or f"uid-{episode_id}-{attempt}", config=config,
        observability={"partial": True} if partial else {},
        stopped=partial,
    )
    manifest = EpisodeManifest.from_run(
        config=config.model_dump(), experiment_name=config.experiment_name or "",
        cell_id=episode_id.split(".")[1], episode_idx=0, episode_uid=trace.episode_uid,
    )
    manifest.episode_id = episode_id
    return SQLiteEpisodeStore(path=control.path).put_episode(trace, manifest)


def _fixture(tmp_path: Path) -> tuple[ControlPlane, str]:
    """A launch with a completed episode, a partial one, and two with nothing."""
    control = _control(tmp_path)
    experiment = _experiment(control)
    control.launcher = FakeLauncher()
    launch = control.launch_experiment(experiment.id, smoke_test=True)
    planned = control.planned_episode_ids(launch.id)
    assert len(planned) == 4
    _record_episode(control, planned[0])
    _record_episode(control, planned[1], partial=True)
    return control, launch.id


# --- the two join implementations must agree ---------------------------------


def test_both_launch_truth_implementations_agree_on_the_same_fixture(tmp_path):
    control, launch_id = _fixture(tmp_path)

    assert control._completed_executions_sql(launch_id) == \
        control._completed_executions_two_step(launch_id)
    assert control._progress_rows_sql(launch_id) == \
        control._progress_rows_two_step(launch_id)

    # And the answer is the one the fixture describes: the partial episode is
    # evidence, not a result, so it is not counted.
    progress = control._progress_rows_two_step(launch_id)
    assert (progress["planned"], progress["completed"]) == (4, 1)


def test_both_implementations_agree_when_an_attempt_number_moves(tmp_path):
    """The join is on ``(episode_id, attempt)``, not on ``episode_id`` alone.

    A second launch of the same design bumps ``attempt``, so the first launch's
    completed episode must not settle the second launch's attempt.
    """
    control, first = _fixture(tmp_path)
    experiment = control.experiments()[0]
    second = control.launch_experiment(experiment.id, smoke_test=True)
    planned = control.planned_episode_ids(second.id)
    _record_episode(control, planned[0], attempt=2)

    for launch_id in (first, second.id):
        assert control._completed_executions_sql(launch_id) == \
            control._completed_executions_two_step(launch_id)
        assert control._progress_rows_sql(launch_id) == \
            control._progress_rows_two_step(launch_id)

    completed = control._completed_executions_two_step(second.id)
    assert list(completed) == [(planned[0], 2)]


def test_a_relaunch_is_not_credited_with_an_earlier_launchs_results(tmp_path):
    """Settlement and progress are both attempt-level.

    A re-launch of a design that already has results plans a *new* attempt for
    every episode, and the dispatched worker never runs with ``--resume``, so it
    re-executes all of them. An earlier launch's completed episode for the same
    id is not work this launch has done: counting it made a live re-launch read
    ``2/2`` in the browser while both of its attempts were still running.
    """
    control, first = _fixture(tmp_path)
    experiment = control.experiments()[0]
    second = control.launch_experiment(experiment.id, smoke_test=True)
    planned = control.planned_episode_ids(second.id)
    # Deliberately nothing recorded for the second launch's attempts.

    assert control._completed_executions_sql(second.id) == \
        control._completed_executions_two_step(second.id) == {}
    progress_sql = control._progress_rows_sql(second.id)
    assert progress_sql == control._progress_rows_two_step(second.id)
    assert progress_sql["completed"] == 0
    # The first launch still reads as done: the fix narrows, it does not drop.
    assert control._progress_rows_sql(first)["completed"] == 1
    # ...and it counts for the episode it is, not for whichever one ran first.
    assert {attempt["attempt"] for attempt in control.launch_detail(second.id)["attempts"]} == {2}
    assert set(planned) == set(control.planned_episode_ids(first))


def test_a_launch_settles_identically_through_either_implementation(tmp_path):
    """The whole settlement, not only its input, must be implementation-blind."""

    def settle(colocated: bool) -> list[tuple[str, str, str | None]]:
        control, launch_id = _fixture(tmp_path / ("colocated" if colocated else "split"))
        control.colocated_reads = colocated
        control.launcher.finish(launch_id)
        detail = control.launch_detail(launch_id)
        return [
            (detail["launch"]["status"], *sorted(
                (attempt["episode_id"].split(".", 1)[1], attempt["status"])
                for attempt in detail["attempts"]
            )[0]),
        ]

    assert settle(True) == settle(False)


def test_the_two_step_asks_the_store_rather_than_reading_its_table(tmp_path):
    """The reference implementation must not reach into ``episodes`` itself.

    If it did, it would be the single-statement join with extra steps, and
    swapping the episode store for something that is not a table in this file
    would silently stop settling launches.
    """
    control, launch_id = _fixture(tmp_path)
    asked: list[list[str] | None] = []
    real = SQLiteEpisodeStore.completed_executions

    def recording(self, episode_ids=None):
        asked.append(None if episode_ids is None else list(episode_ids))
        return real(self, episode_ids)

    SQLiteEpisodeStore.completed_executions = recording
    try:
        control._completed_executions_two_step(launch_id)
    finally:
        SQLiteEpisodeStore.completed_executions = real

    assert asked, "the two-step never asked the episode store what it holds"
    assert set(asked[0]) == set(control.planned_episode_ids(launch_id))


# --- resolution by name -------------------------------------------------------


def test_the_resolved_local_reader_satisfies_the_control_plane_protocol(tmp_path):
    control = _control(tmp_path)

    assert isinstance(control._store(), ControlPlaneReader)
    LocalStackHandler.database = tmp_path / "a2a.db"
    assert isinstance(LocalStackHandler._store(), ControlPlaneReader)


def test_a_backend_that_cannot_serve_a_control_plane_is_refused(tmp_path):
    """Loudly, rather than as an episode page beside an empty browser."""
    with pytest.raises(TypeError) as excinfo:
        make_control_plane_reader({"backend": "local"}, results_dir=tmp_path)
    assert "episode_summaries" in str(excinfo.value)


def test_the_episode_browser_reads_identically_before_and_after_the_split(tmp_path):
    """Resolution changed; the payload must not have.

    Exercised with the whole read surface the browser uses -- the free-text
    ``q``, a multi-valued filter, a single-valued one, and the facets -- because
    those are the methods that are not on the four-method ``EpisodeStore``
    protocol and so are the ones the split could have dropped.
    """
    control, _ = _fixture(tmp_path)
    for index in range(3):
        _record_episode(control, f"Split.cell-{index}.000", uid=f"split-{index}")

    query = {
        "q": ["split"],
        "environment_id": ["buyer_seller", "word_guess"],
        "experiment_name": ["Split"],
        "limit": ["50"],
    }
    LocalStackHandler.database = control.path

    imported = SQLiteEpisodeStore(path=control.path)
    resolved = LocalStackHandler._store()

    filters = {"q": "split", "environment_id": ["buyer_seller", "word_guess"],
               "experiment_name": "Split"}
    assert json.dumps(resolved.episode_summaries(filters, limit=50), sort_keys=True, default=str) \
        == json.dumps(imported.episode_summaries(filters, limit=50), sort_keys=True, default=str)
    assert json.dumps(resolved.episode_facets(filters), sort_keys=True, default=str) \
        == json.dumps(imported.episode_facets(filters), sort_keys=True, default=str)
    assert resolved.count_episodes(filters) == imported.count_episodes(filters)

    page = LocalStackHandler._episode_page(query)
    assert {row["episode_uid"] for row in page["episodes"]} == \
        {f"split-{index}" for index in range(3)}
    assert page["facets"]


def test_the_lane_roster_join_survives_the_two_stores_resolving_separately(tmp_path):
    """``_lanes`` already joins a store trace against a control-plane roster.

    It is the pattern the launch-truth two-step is modelled on, so it has to
    keep working once the episode store is resolved by name rather than
    constructed in the module.
    """
    control = _control(tmp_path)
    experiment = control.create_experiment(
        name="Lane split", release_id="buyer_seller", design_text=LANE_DESIGN,
    )
    control.lock_experiment(experiment.id, design_sha256=experiment.design_sha256 or "")
    config = control._design_episode_configs(control.experiment(experiment.id), mode="smoke")[0]
    episode = EpisodeConfigBase.model_validate(config)
    trace = EpisodeTrace(episode_uid="lane-1", config=episode)
    manifest = EpisodeManifest.from_run(
        config=episode.model_dump(), experiment_name=str(episode.experiment_name or ""),
        cell_id=str(config["provenance"]["cell_id"]), episode_idx=0, episode_uid="lane-1",
    )
    manifest.episode_id = str(episode.episode_id)
    SQLiteEpisodeStore(path=control.path).put_episode(trace, manifest)

    LocalStackHandler.database = control.path
    LocalStackHandler._control_plane = None
    LocalStackHandler.workspace = WORKSPACE
    detail = LocalStackHandler._episode_detail("lane-1")

    assert detail is not None
    assert {lane["participant_id"] for lane in detail["lanes"]} == {"seller", "buyer"}
    # The roster half of the join is the control plane's, and it still arrives.
    assert {lane["role"] for lane in detail["lanes"]} == {"seller", "buyer"}


LANE_DESIGN = """schema_version: 1
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


# --- and the shortcut is structurally unavailable ------------------------------


def test_compose_gives_the_worker_a_results_directory_of_its_own():
    """The control plane must not be able to read the worker's files.

    ``_recover_partial`` used to work only because it could. Locally that was
    invisible -- one volume, one path -- so the assertion has to be on the
    Compose file: evidence crosses through the artifact root and nowhere else.
    """
    compose = yaml.safe_load((WORKSPACE / "docker-compose.yml").read_text())

    viewer = compose["services"]["viewer"]["command"]
    database = Path(viewer[viewer.index("--database") + 1])
    control_plane_results = database.parent / "results"
    artifact_root = Path(viewer[viewer.index("--artifact-root") + 1])

    runner = compose["services"]["runner"]["command"]
    worker_results = Path(runner[runner.index("--results-dir") + 1])

    assert worker_results != control_plane_results
    assert control_plane_results not in worker_results.parents
    assert worker_results not in artifact_root.parents, (
        "the artifact root must not live inside the worker's results tree"
    )
    # The dispatched worker gets the same treatment as the Compose smoke job.
    assert worker_results.name == WORKER_RESULTS_DIRNAME

    # The one path both sides touch, and it is shared on purpose.
    for service in ("runner", "viewer"):
        assert "a2a-data:/data" in compose["services"][service]["volumes"]
    assert str(artifact_root).startswith("/data/")
