"""Trace-derived rating replay and SQLite materialization seams."""

from __future__ import annotations

from a2a_engine.derived import DerivedArtifact, trace_digest
from a2a_engine.manifest import RunManifest
from a2a_engine.ratings import MetricSpec, RatingEvent, RatingParticipant, rebuild_rating_snapshot
from a2a_engine.schemas import AgentInfo, GameConfigBase, GameTraceBase
from a2a_engine.storage.sqlite import SQLiteTraceStore


class DemoRatingAdapter:
    game_name = "demo"
    version = "demo-rating-v1"
    metrics = [
        MetricSpec(name="quality", higher_is_better=True),
        MetricSpec(name="privacy", higher_is_better=False),
    ]

    def extract(self, trace, artifacts):
        scores = {"quality": {"agent_0": 1.0, "agent_1": 0.0}}
        artifact = artifacts.get("demo.privacy@v1")
        if artifact is not None:
            scores["privacy"] = artifact.payload["scores"]
        return RatingEvent(
            game_id=trace["game_id"],
            game_name="demo",
            participants=[
                RatingParticipant(participant_id="agent_0", player_id="model-a"),
                RatingParticipant(participant_id="agent_1", player_id="model-b"),
            ],
            metric_scores=scores,
        )


def _put(store: SQLiteTraceStore, game_id: str) -> GameTraceBase:
    config = GameConfigBase(
        game_name="demo", num_agents=2,
        agents=[AgentInfo(model="model-a"), AgentInfo(model="model-b")],
        experiment_name="test", experiment_run_id=f"test.batch.{game_id}",
    )
    trace = GameTraceBase(game_id=game_id, config=config, metrics={"quality": 1.0})
    manifest = RunManifest.from_run(
        config=config.model_dump(), experiment_name="test", batch_label="batch",
        run_idx=0, game_id=game_id,
    )
    store.put_trace(trace, manifest)
    return store.get_trace(game_id)  # use the exact persisted source record


def test_replay_persists_events_and_suppresses_unavailable_metrics(tmp_path):
    store = SQLiteTraceStore(path=tmp_path / "traces.db")
    _put(store, "g1")
    _put(store, "g2")

    result = rebuild_rating_snapshot(store, DemoRatingAdapter())

    assert [metric.name for metric in result.snapshot.metrics] == ["quality"]
    assert result.suppressed_metric_names == ("privacy",)
    assert len(result.events) == 2
    assert len(list(store.iter_rating_events(game_name="demo", adapter_version="demo-rating-v1"))) == 2
    persisted = store.get_rating_snapshot(game_name="demo", adapter_version="demo-rating-v1")
    assert persisted is not None
    assert [metric.name for metric in persisted.metrics] == ["quality"]


def test_artifacts_are_digest_bound_and_make_metric_eligible(tmp_path):
    store = SQLiteTraceStore(path=tmp_path / "traces.db")
    for game_id in ("g1", "g2"):
        trace = _put(store, game_id)
        assert store.put_derived_artifact(DerivedArtifact(
            game_id=game_id,
            kind="demo.privacy",
            version="v1",
            trace_digest=trace_digest(trace),
            payload={"scores": {"agent_0": 0.0, "agent_1": 1.0}},
        ))

    result = rebuild_rating_snapshot(store, DemoRatingAdapter())

    assert [metric.name for metric in result.snapshot.metrics] == ["quality", "privacy"]
    assert result.suppressed_metric_names == ()
    assert store.put_derived_artifact(store.get_derived_artifacts("g1")[0]) is False


def test_replacing_a_trace_invalidates_its_derived_records(tmp_path):
    store = SQLiteTraceStore(path=tmp_path / "traces.db")
    trace = _put(store, "g1")
    store.put_derived_artifact(DerivedArtifact(
        game_id="g1", kind="demo.privacy", version="v1",
        trace_digest=trace_digest(trace), payload={"scores": {}},
    ))
    rebuild_rating_snapshot(store, DemoRatingAdapter())
    assert store.get_derived_artifacts("g1")
    assert list(store.iter_rating_events(game_name="demo", adapter_version="demo-rating-v1"))

    trace.metrics["quality"] = 2.0
    manifest = RunManifest.from_run(
        config=trace.config.model_dump(), experiment_name="test", batch_label="batch",
        run_idx=0, game_id="g1",
    )
    store.put_trace(trace, manifest)

    assert store.get_derived_artifacts("g1") == []
    assert list(store.iter_rating_events(game_name="demo", adapter_version="demo-rating-v1")) == []
