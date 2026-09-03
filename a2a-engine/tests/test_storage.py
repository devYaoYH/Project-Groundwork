"""Seam tests: the EpisodeStore abstraction.

Storage moved from hardcoded S3 calls inside expt-runner to a pluggable backend.
These pin the behaviors that migration could quietly break:

- local layout matches the pre-merge one (tooling globs for these paths)
- a remote failure never loses the local trace or fails the run
- Firestore's compression and nested-array flattening round-trip exactly
"""

import json
from pathlib import Path

import pytest

from a2a_engine.manifest import EpisodeManifest
from a2a_engine.schemas import EpisodeConfigBase, Event, EpisodeTrace
from a2a_engine.storage import make_store
from a2a_engine.storage.firestore import (
    FirestoreEpisodeStore,
    compress_events,
    decompress_events,
    flatten_nested_arrays,
    restore_nested_arrays,
)
from a2a_engine.storage.local import LocalJSONStore
from a2a_engine.storage.s3 import S3EpisodeStore


def make_trace(episode_uid="g1", environment_id="demo"):
    return EpisodeTrace(
        episode_uid=episode_uid,
        config=EpisodeConfigBase(environment_id=environment_id, num_agents=2),
        events=[
            Event(type="message", data={"speaker": "agent_a", "text": "hello"}),
            Event(type="round_complete", data={"round": 1}),
        ],
        final_state={"winner": "agent_a", "agent_projects": [[{"n": 1}], [{"n": 2}]]},
        metrics={"fairness": 0.5},
    )


def make_manifest(episode_uid="g1", experiment="exp1"):
    return EpisodeManifest.from_run(
        config={"environment_id": "demo", "episode_id": f"{experiment}.b.0", "seed": 3},
        experiment_name=experiment,
        cell_id="b",
        episode_idx=0,
        episode_uid=episode_uid,
    )


# --- local store -------------------------------------------------------------


def test_local_store_writes_expected_layout(tmp_path):
    """Paths are load-bearing: calendar's analysis scripts glob for them."""
    store = LocalJSONStore(results_dir=tmp_path)
    uri = store.put_episode(make_trace(), make_manifest())

    trace_path = Path(uri)
    assert trace_path == tmp_path / "exp1" / "g1.json"
    assert trace_path.with_name("g1.manifest.json").exists()
    # Compatibility alias for pre-merge tooling.
    assert trace_path.with_name("g1.metadata.json").exists()
    assert (tmp_path / "exp1" / "_run_manifest.jsonl").exists()


def test_local_store_records_written_status(tmp_path):
    store = LocalJSONStore(results_dir=tmp_path)
    manifest = make_manifest()
    store.put_episode(make_trace(), manifest)
    assert manifest.storage.status == "written"
    assert manifest.trace_size_bytes > 0


def test_local_store_round_trips_trace(tmp_path):
    store = LocalJSONStore(results_dir=tmp_path)
    store.put_episode(make_trace(), make_manifest())
    loaded = store.get_episode("g1")
    assert loaded is not None
    assert loaded.final_state["winner"] == "agent_a"
    assert [e.type for e in loaded.events] == ["message", "round_complete"]


def test_completed_episode_ids_supports_resume(tmp_path):
    store = LocalJSONStore(results_dir=tmp_path)
    store.put_episode(make_trace(), make_manifest())
    assert store.completed_episode_ids("exp1") == {"exp1.b.0"}


def test_completed_episode_ids_ignores_runs_whose_trace_was_deleted(tmp_path):
    """A manifest without its trace is not a completed run — it must re-run."""
    store = LocalJSONStore(results_dir=tmp_path)
    uri = store.put_episode(make_trace(), make_manifest())
    Path(uri).unlink()
    for alias in ("g1.manifest.json", "g1.metadata.json"):
        (tmp_path / "exp1" / alias).unlink()
    assert store.completed_episode_ids("exp1") == set()


def test_local_list_episodes_filters_and_paginates(tmp_path):
    store = LocalJSONStore(results_dir=tmp_path)
    for i in range(3):
        store.put_episode(make_trace(episode_uid=f"g{i}"), make_manifest(episode_uid=f"g{i}"))
    rows, cursor = store.list_episodes(limit=2)
    assert len(rows) == 2 and cursor == "2"
    rows, cursor = store.list_episodes(limit=2, cursor=cursor)
    assert len(rows) == 1 and cursor is None
    rows, _ = store.list_episodes(filters={"environment_id": "nope"})
    assert rows == []


# --- remote failure isolation ------------------------------------------------


def test_s3_failure_preserves_local_trace_and_does_not_raise(tmp_path, monkeypatch):
    """The pre-merge runner swallowed upload errors; that must still hold."""
    store = S3EpisodeStore(bucket="nonexistent", results_dir=tmp_path)

    def boom(*_args, **_kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(store, "_upload", boom)

    manifest = make_manifest()
    uri = store.put_episode(make_trace(), manifest)

    assert Path(uri).exists(), "local trace must survive a failed upload"
    assert manifest.storage.status == "failed"
    assert "network down" in manifest.storage.error
    # The failure is durable on disk, not just in memory.
    on_disk = json.loads((tmp_path / "exp1" / "g1.manifest.json").read_text())
    assert on_disk["storage"]["status"] == "failed"


def test_s3_without_bucket_degrades_to_local(tmp_path, monkeypatch):
    monkeypatch.delenv("A2A_TRACE_BUCKET", raising=False)
    store = S3EpisodeStore(results_dir=tmp_path)
    manifest = make_manifest()
    uri = store.put_episode(make_trace(), manifest)
    assert Path(uri).exists()
    assert manifest.storage.status == "not_configured"


def test_s3_key_layout_is_unchanged(tmp_path, monkeypatch):
    """Existing bucket contents stay addressable only if the key layout holds."""
    store = S3EpisodeStore(
        bucket="b", prefix="calendar-episodes", uploader="alice", results_dir=tmp_path
    )
    seen = []
    monkeypatch.setattr(store, "_upload", lambda p, key: seen.append(key))
    store.put_episode(make_trace(), make_manifest())
    assert seen[0] == "calendar-episodes/alice/exp1/exp1.b.0/g1.json"
    assert seen[1] == "calendar-episodes/alice/exp1/exp1.b.0/g1.manifest.json"


def test_firestore_without_client_degrades_to_local(tmp_path):
    store = FirestoreEpisodeStore(results_dir=tmp_path)
    store._client = None
    manifest = make_manifest()
    uri = store.put_episode(make_trace(), manifest)
    assert Path(uri).exists()
    assert manifest.storage.status == "not_configured"


# --- firestore encoding ------------------------------------------------------


def test_event_compression_round_trips():
    events = [{"type": "cheap_talk", "data": {"speaker": "agent_a", "text": "hi"}}]
    assert decompress_events(compress_events(events)) == events


@pytest.mark.parametrize(
    "value",
    [
        {"agent_projects": [[{"n": 1}], [{"n": 2}]]},
        {"flat": [1, 2, 3]},
        {"nested": {"deep": [[1], [2, 3]]}},
        {"empty": []},
        {"scalar": 5, "text": "x"},
    ],
)
def test_nested_array_flattening_round_trips(value):
    """Firestore rejects list[list[...]]; the fix must be lossless."""
    assert restore_nested_arrays(flatten_nested_arrays(value)) == value


def test_flatten_rewrites_only_nested_lists():
    out = flatten_nested_arrays({"a": [[1], [2]], "b": [1, 2]})
    assert out == {"a": {"0": [1], "1": [2]}, "b": [1, 2]}


def test_firestore_document_round_trips_a_trace(tmp_path):
    store = FirestoreEpisodeStore(results_dir=tmp_path)
    trace, manifest = make_trace(), make_manifest()
    doc = store.to_document(trace, manifest)

    assert "events" not in doc, "events must be stored compressed"
    assert doc["final_state"]["agent_projects"] == {"0": [{"n": 1}], "1": [{"n": 2}]}

    restored = store.from_document(doc)
    assert restored.episode_uid == trace.episode_uid
    assert restored.final_state == trace.final_state
    assert [e.type for e in restored.events] == [e.type for e in trace.events]
    assert restored.events[0].data["text"] == "hello"


def test_firestore_store_lifts_the_legacy_negotiation_envelope(tmp_path):
    store = FirestoreEpisodeStore(results_dir=tmp_path)
    legacy = {
        "schema_version": 5,
        "episode_uid": "legacy-negotiation",
        "game_config": {
            "mode": "stable",
            "num_rounds": 1,
            "agents": [{"type": "llm", "model": "a"}, {"type": "llm", "model": "b"}],
            "agent_projects": {"0": [{"name": "a"}], "1": [{"name": "b"}]},
        },
        "result": {
            "episode_uid": "legacy-negotiation",
            "rounds": [{"round_number": 1, "overdrawn": False}],
            "agent_a_cumulative_reward": 3.0,
            "agent_b_cumulative_reward": 5.0,
        },
        "events_compressed": compress_events([{ "type": "message", "data": {"text": "hi"} }]),
    }

    restored = store.from_document(legacy)

    assert restored.config.environment_id == "negotiation"
    assert restored.config.num_agents == 2
    assert restored.final_state["agent_a_cumulative_reward"] == 3.0
    assert restored.metrics["joint_reward"] == 8.0
    assert restored.metrics["overdrawn_rounds"] == 0
    assert restored.events[0].data["text"] == "hi"


# --- factory -----------------------------------------------------------------


def test_make_store_defaults_to_local(tmp_path):
    assert make_store(None, results_dir=tmp_path).name == "local"
    assert make_store({}, results_dir=tmp_path).name == "local"


def test_make_store_selects_backend_and_passes_options(tmp_path):
    store = make_store(
        {"backend": "s3", "bucket": "mybucket", "prefix": "p"}, results_dir=tmp_path
    )
    assert store.name == "s3" and store.bucket == "mybucket" and store.prefix == "p"


def test_make_store_rejects_unknown_backend(tmp_path):
    with pytest.raises(KeyError, match="Unknown trace store backend"):
        make_store({"backend": "postgres"}, results_dir=tmp_path)
