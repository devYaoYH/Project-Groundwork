from a2a_engine.derived_metrics import (
    DerivedMetricResult,
    materialize_derived_metrics,
    register_derived_metric_extractor,
)
from a2a_engine.manifest import EpisodeManifest
from a2a_engine.schemas import ReleaseReference, EpisodeConfigBase, EpisodeTrace
from a2a_engine.storage.sqlite import SQLiteEpisodeStore


class _QualityExtractor:
    identifier = "test.quality"
    version = "v1"

    def extract(self, trace, artifacts):
        return DerivedMetricResult(values={"quality": 0.75}, metadata={"source": "test"})


def test_declared_derived_metrics_materialize_as_idempotent_artifacts(tmp_path):
    register_derived_metric_extractor(_QualityExtractor())
    config = EpisodeConfigBase(environment_id="demo", num_agents=2, experiment_name="e", episode_id="e.b.0")
    trace = EpisodeTrace(
        episode_uid="g1", config=config,
        release=ReleaseReference(
            id="demo.tiny", release="v1", content_sha256="a" * 64,
            metrics=[{
                "name": "quality", "producer": "derived", "extractor": "test.quality",
                "direction": "maximize",
            }],
        ),
    )
    store = SQLiteEpisodeStore(path=tmp_path / "episodes.db")
    store.put_episode(trace, EpisodeManifest.from_run(
        config=config.model_dump(), experiment_name="e", cell_id="b", episode_idx=0, episode_uid="g1",
    ))

    first = materialize_derived_metrics(store)
    assert len(first) == 1 and first[0].changed
    assert first[0].artifact and first[0].artifact.payload["values"] == {"quality": 0.75}
    assert store.get_derived_artifacts("g1")[0].kind == "derived_metrics.test.quality"

    second = materialize_derived_metrics(store)
    assert len(second) == 1 and not second[0].changed
