import base64

import pytest

from a2a_engine.content import AssetStore
from a2a_engine.environment import ParameterConfig
from a2a_engine.episode_media import persist_episode_images, read_episode_image
from a2a_engine.manifest import EpisodeManifest
from a2a_engine.schemas import EpisodeTrace
from a2a_engine.storage.sqlite import SQLiteEpisodeStore

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=")


def test_episode_generated_images_survive_source_removal_and_are_trace_scoped(tmp_path):
    source = AssetStore(tmp_path / "generated")
    image = source.put(PNG, "image/png")
    config = {"environment_id": "fixture", "num_agents": 2, "asset_root": str(source.root)}
    trace = EpisodeTrace(episode_uid="visual", config=config,
                         events=[{"type": "observation", "data": {"speaker": "follower", "content": [image.model_dump()]}}])
    manifest = EpisodeManifest.from_run(config=config, experiment_name="visual", cell_id="cell", episode_idx=0, episode_uid="visual")
    store = SQLiteEpisodeStore(path=tmp_path / "evidence" / "trace.db")
    store.put_episode(trace, manifest)
    (source.root / image.sha256).unlink()
    replay = store.get_episode("visual")
    assert read_episode_image(replay, image.sha256, tmp_path / "evidence" / "assets") == (PNG, "image/png")
    assert manifest.image_assets[0]["sha256"] == image.sha256
    assert "data" not in manifest.image_assets[0]
    unrelated = AssetStore(tmp_path / "evidence" / "assets").put(PNG + b"other", "image/png")
    with pytest.raises(KeyError, match="not referenced"):
        read_episode_image(replay, unrelated.sha256, tmp_path / "evidence" / "assets")


def test_corrupt_generated_input_is_not_saved_as_replay_evidence(tmp_path):
    source = AssetStore(tmp_path / "generated")
    image = source.put(PNG, "image/png")
    (source.root / image.sha256).write_bytes(PNG + b"corrupt")
    trace = EpisodeTrace(episode_uid="corrupt", config={"environment_id": "fixture", "num_agents": 2, "asset_root": str(source.root)},
                         events=[{"type": "observation", "data": {"content": [image.model_dump()]}}])
    with pytest.raises(ValueError, match="digest mismatch"):
        persist_episode_images(trace, tmp_path / "evidence")


def test_numeric_yaml_domains_normalize_but_categorical_domains_stay_lists():
    assert ParameterConfig.model_validate({"name": "rounds", "type": "integer", "domain": [2, 6]}).domain == (2, 6)
    assert ParameterConfig.model_validate({"name": "mode", "type": "categorical", "domain": ["a", "b"]}).domain == ["a", "b"]
    with pytest.raises(ValueError):
        ParameterConfig.model_validate({"name": "rounds", "type": "integer", "domain": [2, 6, 9]})
