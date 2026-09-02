"""Tests for the SQLite trace sink.

Focused on the properties the runner depends on: durable round-trip, resume
support, filtered listing, concurrent writes from the thread pool, and a check()
that actually fails when the sink is unusable.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from a2a_engine.manifest import RunManifest
from a2a_engine.schemas import (
    EnvironmentReference,
    EpisodeReference,
    GameConfigBase,
    GameEvent,
    GameTraceBase,
)
from a2a_engine.storage import check_store, make_store
from a2a_engine.storage.sqlite import SQLiteTraceStore


def make_trace(game_id: str, *, game_name: str = "demo", run_id: str = "e.b.0",
               events: int = 2) -> tuple[GameTraceBase, RunManifest]:
    cfg = GameConfigBase(
        game_name=game_name, num_agents=2,
        experiment_name="e", experiment_run_id=run_id,
    )
    trace = GameTraceBase(
        game_id=game_id,
        config=cfg,
        events=[
            GameEvent(type="message", data={"speaker": "a", "text": f"msg {i}"})
            for i in range(events)
        ],
        final_state={"done": True},
        metrics={"score": 1.5},
    )
    manifest = RunManifest.from_run(
        config=cfg.model_dump(), experiment_name="e", batch_label="b",
        run_idx=0, game_id=game_id,
    )
    manifest.experiment_run_id = run_id
    manifest.game_name = game_name
    return trace, manifest


@pytest.fixture
def store(tmp_path) -> SQLiteTraceStore:
    return SQLiteTraceStore(path=tmp_path / "traces.db", results_dir=tmp_path)


def test_round_trip_preserves_trace(store):
    trace, manifest = make_trace("g1")
    store.put_trace(trace, manifest)

    back = store.get_trace("g1")
    assert back is not None
    assert back.game_id == "g1"
    assert len(back.events) == 2
    assert back.metrics == {"score": 1.5}
    assert back.final_state == {"done": True}
    assert back.config.game_name == "demo"


def test_round_trip_preserves_typed_environment_and_episode_identity(store):
    trace, manifest = make_trace("g1")
    trace.environment = EnvironmentReference(
        id="demo.calendar", revision="v1", content_sha256="a" * 64,
        inputs=[{"id": "task", "path": "task.json", "sha256": "b" * 64}],
    )
    trace.episode = EpisodeReference(
        id="e.b.0", experiment_name="e", batch_label="b", run_idx=0,
    )
    store.put_trace(trace, manifest)

    restored = store.get_trace("g1")
    assert restored and restored.environment and restored.episode
    assert restored.environment.id == "demo.calendar"
    assert restored.episode.id == "e.b.0"


def test_manifest_records_written_status_and_uri(store):
    trace, manifest = make_trace("g1")
    uri = store.put_trace(trace, manifest)

    assert manifest.storage.backend == "sqlite"
    assert manifest.storage.status == "written"
    assert manifest.storage.uri == uri
    assert uri.startswith("sqlite:///")


def test_missing_trace_returns_none(store):
    assert store.get_trace("nope") is None


def test_list_traces_filters_on_indexed_columns(store):
    store.put_trace(*make_trace("g1", game_name="alpha", run_id="e.b.0"))
    store.put_trace(*make_trace("g2", game_name="beta", run_id="e.b.1"))

    rows, _ = store.list_traces({"game_name": "alpha"})
    assert len(rows) == 1
    assert rows[0]["game_id"] == "g1"


def test_unknown_filter_keys_are_ignored_not_interpolated(store):
    """A filter key must never reach the SQL string."""
    store.put_trace(*make_trace("g1"))
    rows, _ = store.list_traces({"1=1; DROP TABLE traces": "x"})
    assert len(rows) == 1  # filter ignored, table intact
    assert store.get_trace("g1") is not None


def test_list_traces_paginates(store):
    for i in range(5):
        store.put_trace(*make_trace(f"g{i}", run_id=f"e.b.{i}"))

    page1, cursor = store.list_traces(limit=2)
    assert len(page1) == 2 and cursor is not None
    page2, cursor2 = store.list_traces(limit=2, cursor=cursor)
    assert len(page2) == 2
    page3, cursor3 = store.list_traces(limit=2, cursor=cursor2)
    assert len(page3) == 1 and cursor3 is None


def test_completed_run_ids_supports_resume(store):
    store.put_trace(*make_trace("g1", run_id="e.b.0"))
    store.put_trace(*make_trace("g2", run_id="e.b.1"))
    assert store.completed_run_ids("e") == {"e.b.0", "e.b.1"}
    assert store.completed_run_ids("other") == set()


def test_rewriting_same_game_id_replaces_rather_than_duplicates(store):
    store.put_trace(*make_trace("g1", events=2))
    store.put_trace(*make_trace("g1", events=5))
    rows, _ = store.list_traces()
    assert len(rows) == 1
    assert len(store.get_trace("g1").events) == 5


def test_concurrent_writes_all_land(store):
    """The runner fans out across threads; SQLite must not drop or corrupt rows."""
    payloads = [make_trace(f"g{i}", run_id=f"e.b.{i}") for i in range(24)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda p: store.put_trace(*p), payloads))

    rows, _ = store.list_traces(limit=100)
    assert len(rows) == 24
    assert {r["game_id"] for r in rows} == {f"g{i}" for i in range(24)}


def test_iter_traces_streams_every_row(store):
    for i in range(3):
        store.put_trace(*make_trace(f"g{i}", run_id=f"e.b.{i}"))
    assert len({t.game_id for t in store.iter_traces()}) == 3


def test_check_passes_on_a_usable_database(store):
    result = check_store(store)
    assert result.ok
    assert result.backend == "sqlite"
    assert result.latency_ms is not None


def test_check_fails_when_the_path_is_unusable(tmp_path):
    """A corrupt file is the realistic failure; the probe must catch it."""
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"this is definitely not a sqlite database" * 10)
    result = check_store(SQLiteTraceStore(path=bad, results_dir=tmp_path))
    assert not result.ok
    assert "DatabaseError" in result.detail or "malformed" in result.detail.lower()


def test_check_leaves_no_canary_behind(store):
    check_store(store)
    rows, _ = store.list_traces(limit=100)
    assert rows == []


def test_make_store_builds_sqlite_from_a_config_block(tmp_path):
    store = make_store(
        {"backend": "sqlite", "path": str(tmp_path / "x.db")}, results_dir=tmp_path
    )
    assert store.name == "sqlite"
    assert check_store(store).ok


def test_mirror_json_writes_both_records(tmp_path):
    store = SQLiteTraceStore(
        path=tmp_path / "t.db", results_dir=tmp_path, mirror_json=True
    )
    trace, manifest = make_trace("g1")
    store.put_trace(trace, manifest)

    assert store.get_trace("g1") is not None
    assert manifest.local_trace_path is not None
    assert list(tmp_path.rglob("g1.json"))


def test_default_path_lands_under_results_dir(tmp_path):
    store = SQLiteTraceStore(results_dir=tmp_path)
    store.put_trace(*make_trace("g1"))
    assert store.path.parent == tmp_path
    assert store.path.exists()


def test_schema_is_queryable_with_plain_sqlite(store):
    """The point of the DB sink is that any SQLite client can read it."""
    store.put_trace(*make_trace("g1", game_name="alpha"))
    conn = sqlite3.connect(store.path)
    try:
        row = conn.execute(
            "SELECT game_name, experiment_name FROM traces WHERE game_id = ?", ("g1",)
        ).fetchone()
    finally:
        conn.close()
    assert row == ("alpha", "e")
