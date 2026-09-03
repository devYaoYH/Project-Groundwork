from __future__ import annotations

from a2a_engine.manifest import EpisodeManifest
from a2a_engine.registry import get_environment_spec
from a2a_engine.ratings import rebuild_rating_snapshot
from a2a_engine.storage.sqlite import SQLiteEpisodeStore
from calendar_game.artifacts import ingest_calendar_vps_by_game
from calendar_game.game import CalendarGame, CalendarGameConfig
from calendar_game.ratings import CalendarRatingAdapter
from calendar_game.trace_contract import build_calendar_rating_context


def test_completed_calendar_trace_is_self_contained_and_replayable(tmp_path):
    environment = CalendarGame(
        CalendarGameConfig(seed=7, num_agents=2, num_slots=6, num_meetings=1),
        dry_run=True,
    )
    trace = environment.run()
    trace.episode_uid = "calendar-self-contained"
    context = trace.final_state["rating_context"]
    assert context["schema_version"] == 1
    assert context["calendars"]
    assert context["meetings"]

    store = SQLiteEpisodeStore(path=tmp_path / "calendar.db")
    manifest = EpisodeManifest.from_run(
        config=trace.config.model_dump(),
        experiment_name="calendar-test",
        cell_id="smoke",
        episode_idx=0,
        episode_uid=trace.episode_uid,
    )
    store.put_episode(trace, manifest)

    # This path has no task file to read. The rating replay consumes only the
    # persisted trace and, after ingestion, its digest-bound analysis artifact.
    first = rebuild_rating_snapshot(store, CalendarRatingAdapter())
    assert len(first.events) == 1
    assert "excess_vps" in first.suppressed_metric_names

    ingested = ingest_calendar_vps_by_game(
        store,
        {trace.episode_uid: {0: 0.25, 1: 1.5}},
        metadata={"source": "test"},
    )
    assert ingested.written_episode_uids == (trace.episode_uid,)
    assert not ingested.missing_trace_episode_uids
    repeated = ingest_calendar_vps_by_game(
        store,
        {trace.episode_uid: {0: 0.25, 1: 1.5}},
        metadata={"source": "test"},
    )
    assert repeated.unchanged_episode_uids == (trace.episode_uid,)

    second = rebuild_rating_snapshot(store, CalendarRatingAdapter())
    assert "excess_vps" not in second.suppressed_metric_names
    assert [metric.name for metric in second.snapshot.metrics] == [
        "coordination_ratio", "excess_cost", "excess_vps"
    ]


def test_vps_ingestion_is_idempotent_and_rejects_unknown_episode_uids(tmp_path):
    store = SQLiteEpisodeStore(path=tmp_path / "calendar.db")
    result = ingest_calendar_vps_by_game(
        store,
        {"missing": {0: 0.1, 1: 0.2}},
    )
    assert result.missing_trace_episode_uids == ("missing",)


def test_rating_context_is_a_deep_copied_trace_contract():
    scenario = {
        "task_id": "task-7",
        "calendars": [[{"errand_id": 1, "cost": 1}]],
        "meetings": [{"id": 1}],
    }
    context = build_calendar_rating_context(scenario)
    scenario["calendars"][0][0]["cost"] = 100

    assert context == {
        "schema_version": 1,
        "task_id": "task-7",
        "calendars": [[{"errand_id": 1, "cost": 1}]],
        "meetings": [{"id": 1}],
        "prior_meetings": [],
    }


def test_calendar_declares_a_rating_adapter_and_local_sqlite_storage_default():
    spec = get_environment_spec("calendar")
    assert isinstance(spec.rating_adapter, CalendarRatingAdapter)
    assert spec.storage == {"backend": "sqlite"}
