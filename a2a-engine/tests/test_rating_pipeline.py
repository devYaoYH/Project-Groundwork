"""Trace-derived rating replay and SQLite materialization seams."""

from __future__ import annotations

from a2a_engine.derived import DerivedArtifact, trace_digest
from a2a_engine.manifest import EpisodeManifest
from a2a_engine.ratings import MetricSpec, RatingEvent, RatingParticipant, rebuild_rating_snapshot
from a2a_engine.schemas import ParticipantBinding, EpisodeConfigBase, EpisodeTrace
from a2a_engine.storage.sqlite import SQLiteEpisodeStore


class DemoRatingAdapter:
    environment_id = "demo"
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
            episode_uid=trace["episode_uid"],
            environment_id="demo",
            participants=[
                RatingParticipant(participant_id="agent_0", player_id="model-a"),
                RatingParticipant(participant_id="agent_1", player_id="model-b"),
            ],
            metric_scores=scores,
        )


def _put(store: SQLiteEpisodeStore, episode_uid: str) -> EpisodeTrace:
    config = EpisodeConfigBase(
        environment_id="demo", num_agents=2,
        agents=[ParticipantBinding(model="model-a"), ParticipantBinding(model="model-b")],
        experiment_name="test", episode_id=f"test.cell.{episode_uid}",
    )
    trace = EpisodeTrace(episode_uid=episode_uid, config=config, metrics={"quality": 1.0})
    manifest = EpisodeManifest.from_run(
        config=config.model_dump(), experiment_name="test", cell_id="cell",
        episode_idx=0, episode_uid=episode_uid,
    )
    store.put_episode(trace, manifest)
    return store.get_episode(episode_uid)  # use the exact persisted source record


def test_replay_persists_events_and_suppresses_unavailable_metrics(tmp_path):
    store = SQLiteEpisodeStore(path=tmp_path / "episodes.db")
    _put(store, "g1")
    _put(store, "g2")

    result = rebuild_rating_snapshot(store, DemoRatingAdapter())

    assert [metric.name for metric in result.snapshot.metrics] == ["quality"]
    assert result.suppressed_metric_names == ("privacy",)
    assert len(result.events) == 2
    assert len(list(store.iter_rating_events(environment_id="demo", adapter_version="demo-rating-v1"))) == 2
    persisted = store.get_rating_snapshot(environment_id="demo", adapter_version="demo-rating-v1")
    assert persisted is not None
    assert [metric.name for metric in persisted.metrics] == ["quality"]


def test_artifacts_are_digest_bound_and_make_metric_eligible(tmp_path):
    store = SQLiteEpisodeStore(path=tmp_path / "episodes.db")
    for episode_uid in ("g1", "g2"):
        trace = _put(store, episode_uid)
        assert store.put_derived_artifact(DerivedArtifact(
            episode_uid=episode_uid,
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
    store = SQLiteEpisodeStore(path=tmp_path / "episodes.db")
    trace = _put(store, "g1")
    store.put_derived_artifact(DerivedArtifact(
        episode_uid="g1", kind="demo.privacy", version="v1",
        trace_digest=trace_digest(trace), payload={"scores": {}},
    ))
    rebuild_rating_snapshot(store, DemoRatingAdapter())
    assert store.get_derived_artifacts("g1")
    assert list(store.iter_rating_events(environment_id="demo", adapter_version="demo-rating-v1"))

    trace.metrics["quality"] = 2.0
    manifest = EpisodeManifest.from_run(
        config=trace.config.model_dump(), experiment_name="test", cell_id="cell",
        episode_idx=0, episode_uid="g1",
    )
    store.put_episode(trace, manifest)

    assert store.get_derived_artifacts("g1") == []
    assert list(store.iter_rating_events(environment_id="demo", adapter_version="demo-rating-v1")) == []
