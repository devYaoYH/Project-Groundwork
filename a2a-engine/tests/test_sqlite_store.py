"""Tests for the SQLite trace sink.

Focused on the properties the runner depends on: durable round-trip, resume
support, filtered listing, concurrent writes from the thread pool, and a check()
that actually fails when the sink is unusable.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from a2a_engine.manifest import EpisodeManifest
from a2a_engine.schemas import (
    ReleaseReference,
    EpisodeReference,
    EpisodeConfigBase,
    Event,
    EpisodeTrace,
)
from a2a_engine.storage import check_store, make_store
from a2a_engine.storage.schema import SCHEMA, apply_schema
from a2a_engine.storage.sqlite import SQLiteEpisodeStore


def make_trace(episode_uid: str, *, environment_id: str = "demo", run_id: str = "e.b.0",
               events: int = 2) -> tuple[EpisodeTrace, EpisodeManifest]:
    cfg = EpisodeConfigBase(
        environment_id=environment_id, num_agents=2,
        experiment_name="e", episode_id=run_id,
    )
    trace = EpisodeTrace(
        episode_uid=episode_uid,
        config=cfg,
        events=[
            Event(type="message", data={"speaker": "a", "text": f"msg {i}"})
            for i in range(events)
        ],
        final_state={"done": True},
        metrics={"score": 1.5},
    )
    manifest = EpisodeManifest.from_run(
        config=cfg.model_dump(), experiment_name="e", cell_id="b",
        episode_idx=0, episode_uid=episode_uid,
    )
    manifest.episode_id = run_id
    manifest.environment_id = environment_id
    return trace, manifest


@pytest.fixture
def store(tmp_path) -> SQLiteEpisodeStore:
    return SQLiteEpisodeStore(path=tmp_path / "episodes.db", results_dir=tmp_path)


def test_round_trip_preserves_trace(store):
    trace, manifest = make_trace("g1")
    store.put_episode(trace, manifest)

    back = store.get_episode("g1")
    assert back is not None
    assert back.episode_uid == "g1"
    assert len(back.events) == 2
    assert back.metrics == {"score": 1.5}
    assert back.final_state == {"done": True}
    assert back.config.environment_id == "demo"


def test_round_trip_preserves_typed_environment_and_episode_identity(store):
    trace, manifest = make_trace("g1")
    trace.release = ReleaseReference(
        id="demo.calendar", release="v1", content_sha256="a" * 64,
        inputs=[{"id": "task", "path": "task.json", "sha256": "b" * 64}],
    )
    trace.episode = EpisodeReference(
        id="e.b.0", experiment_name="e", cell_id="b", episode_idx=0,
    )
    store.put_episode(trace, manifest)

    restored = store.get_episode("g1")
    assert restored and restored.release and restored.episode
    assert restored.release.id == "demo.calendar"
    assert restored.episode.id == "e.b.0"


def test_manifest_records_written_status_and_uri(store):
    trace, manifest = make_trace("g1")
    uri = store.put_episode(trace, manifest)

    assert manifest.storage.backend == "sqlite"
    assert manifest.storage.status == "written"
    assert manifest.storage.uri == uri
    assert uri.startswith("sqlite:///")


def test_missing_trace_returns_none(store):
    assert store.get_episode("nope") is None


def test_list_episodes_filters_on_indexed_columns(store):
    store.put_episode(*make_trace("g1", environment_id="alpha", run_id="e.b.0"))
    store.put_episode(*make_trace("g2", environment_id="beta", run_id="e.b.1"))

    rows, _ = store.list_episodes({"environment_id": "alpha"})
    assert len(rows) == 1
    assert rows[0]["episode_uid"] == "g1"


def test_unknown_filter_keys_are_ignored_not_interpolated(store):
    """A filter key must never reach the SQL string."""
    store.put_episode(*make_trace("g1"))
    rows, _ = store.list_episodes({"1=1; DROP TABLE episodes": "x"})
    assert len(rows) == 1  # filter ignored, table intact
    assert store.get_episode("g1") is not None


def test_list_episodes_paginates(store):
    for i in range(5):
        store.put_episode(*make_trace(f"g{i}", run_id=f"e.b.{i}"))

    page1, cursor = store.list_episodes(limit=2)
    assert len(page1) == 2 and cursor is not None
    page2, cursor2 = store.list_episodes(limit=2, cursor=cursor)
    assert len(page2) == 2
    page3, cursor3 = store.list_episodes(limit=2, cursor=cursor2)
    assert len(page3) == 1 and cursor3 is None


def test_completed_episode_ids_supports_resume(store):
    store.put_episode(*make_trace("g1", run_id="e.b.0"))
    store.put_episode(*make_trace("g2", run_id="e.b.1"))
    assert store.completed_episode_ids("e") == {"e.b.0", "e.b.1"}
    assert store.completed_episode_ids("other") == set()


def test_rewriting_same_episode_uid_replaces_rather_than_duplicates(store):
    store.put_episode(*make_trace("g1", events=2))
    store.put_episode(*make_trace("g1", events=5))
    rows, _ = store.list_episodes()
    assert len(rows) == 1
    assert len(store.get_episode("g1").events) == 5


def test_concurrent_writes_all_land(store):
    """The runner fans out across threads; SQLite must not drop or corrupt rows."""
    payloads = [make_trace(f"g{i}", run_id=f"e.b.{i}") for i in range(24)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda p: store.put_episode(*p), payloads))

    rows, _ = store.list_episodes(limit=100)
    assert len(rows) == 24
    assert {r["episode_uid"] for r in rows} == {f"g{i}" for i in range(24)}


def test_iter_episodes_streams_every_row(store):
    for i in range(3):
        store.put_episode(*make_trace(f"g{i}", run_id=f"e.b.{i}"))
    assert len({t.episode_uid for t in store.iter_episodes()}) == 3


def test_check_passes_on_a_usable_database(store):
    result = check_store(store)
    assert result.ok
    assert result.backend == "sqlite"
    assert result.latency_ms is not None


def test_check_fails_when_the_path_is_unusable(tmp_path):
    """A corrupt file is the realistic failure; the probe must catch it."""
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"this is definitely not a sqlite database" * 10)
    result = check_store(SQLiteEpisodeStore(path=bad, results_dir=tmp_path))
    assert not result.ok
    assert "DatabaseError" in result.detail or "malformed" in result.detail.lower()


def test_check_leaves_no_canary_behind(store):
    check_store(store)
    rows, _ = store.list_episodes(limit=100)
    assert rows == []


def test_make_store_builds_sqlite_from_a_config_block(tmp_path):
    store = make_store(
        {"backend": "sqlite", "path": str(tmp_path / "x.db")}, results_dir=tmp_path
    )
    assert store.name == "sqlite"
    assert check_store(store).ok


def test_mirror_json_writes_both_records(tmp_path):
    store = SQLiteEpisodeStore(
        path=tmp_path / "t.db", results_dir=tmp_path, mirror_json=True
    )
    trace, manifest = make_trace("g1")
    store.put_episode(trace, manifest)

    assert store.get_episode("g1") is not None
    assert manifest.local_trace_path is not None
    assert list(tmp_path.rglob("g1.json"))


def test_default_path_lands_under_results_dir(tmp_path):
    store = SQLiteEpisodeStore(results_dir=tmp_path)
    store.put_episode(*make_trace("g1"))
    assert store.path.parent == tmp_path
    assert store.path.exists()


def test_schema_is_queryable_with_plain_sqlite(store):
    """The point of the DB sink is that any SQLite client can read it."""
    store.put_episode(*make_trace("g1", environment_id="alpha"))
    conn = sqlite3.connect(store.path)
    try:
        row = conn.execute(
            "SELECT environment_id, experiment_name FROM episodes WHERE episode_uid = ?", ("g1",)
        ).fetchone()
    finally:
        conn.close()
    assert row == ("alpha", "e")


def test_the_merged_file_carries_the_control_plane_tables_too(store):
    """One database is the whole point: a runner invoked with nothing but
    ``--storage-path`` produces a file the control plane can open, and the
    fact table can join its dimensions in SQL rather than in Python."""
    store.put_episode(*make_trace("g1"))
    conn = sqlite3.connect(store.path)
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()
    assert {"episodes", "releases", "items", "experiments", "launches", "attempts",
            "launch_events", "derived_artifacts"} <= tables


def test_foreign_keys_are_enforced_on_the_fact_table(store):
    """A promoted dimension key that resolves to nothing is a broken record,
    and one database means the constraint can say so."""
    store.put_episode(*make_trace("g1"))
    conn = store._connect()
    try:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO episodes (episode_uid, experiment_id, config, events,"
                " final_state, metrics, manifest) VALUES (?,?,?,?,?,?,?)",
                ("orphan", "no-such-experiment", "{}", "[]", "{}", "{}", "{}"),
            )
    finally:
        conn.close()


def test_provenance_is_promoted_into_columns_and_projects_its_release(store):
    trace, manifest = make_trace("g1")
    trace.config.provenance = {
        "experiment_id": None,
        "release_id": "demo",
        "release_version": "v2",
        "declaration_sha256": "d" * 64,
        "item_bank_sha256": "b" * 64,
        "oracle_version": "oracle-v1",
        "attempt": 3,
        "seed": 4242,
        "item_id": None,
    }
    trace.config.seed = 4242
    store.put_episode(trace, manifest)

    conn = sqlite3.connect(store.path)
    try:
        row = conn.execute(
            "SELECT e.attempt, e.seed, e.status, e.release_id, r.version, r.oracle_version"
            " FROM episodes e JOIN releases r ON r.id = e.release_id"
            " WHERE e.episode_uid = ?", ("g1",),
        ).fetchone()
    finally:
        conn.close()
    assert row == (3, 4242, "COMPLETED", "demo", "v2", "oracle-v1")

    # The durable copy stays inside the trace, so the columns are rebuildable.
    restored = store.get_episode("g1")
    assert restored.config.model_extra["provenance"]["release_id"] == "demo"


def test_a_partial_trace_is_evidence_not_a_completed_run(store):
    trace, manifest = make_trace("g1", run_id="e.b.0")
    trace.stopped = True
    trace.observability = {"partial": True, "source": "event_sink_projection"}
    store.put_episode(trace, manifest)

    rows, _ = store.episode_summaries()
    assert rows[0]["status"] == "PARTIAL"
    # ``--resume`` and the progress join must not mistake a recovered fragment
    # for a result and skip re-running it.
    assert store.completed_episode_ids("e") == set()


def test_episode_summaries_page_and_filter_without_rehydrating_traces(store):
    for index in range(5):
        store.put_episode(*make_trace(f"g{index}", run_id=f"e.b.{index}",
                                      environment_id="alpha" if index < 2 else "beta"))

    page, cursor = store.episode_summaries(limit=2)
    assert len(page) == 2 and cursor is not None
    assert set(page[0]) >= {"episode_uid", "episode_id", "attempt", "cell_id",
                            "item_id", "seed", "status", "started_at", "ended_at", "metrics"}
    assert page[0]["metrics"] == {"score": 1.5}

    filtered, _ = store.episode_summaries({"environment_id": "alpha"}, limit=50)
    assert {row["environment_id"] for row in filtered} == {"alpha"}


def test_summary_filters_are_pushed_into_sql_and_unknown_keys_dropped(store):
    store.put_episode(*make_trace("g1"))
    rows, _ = store.episode_summaries({"1=1; DROP TABLE episodes": "x"})
    assert len(rows) == 1
    assert store.get_episode("g1") is not None


def test_episode_search_uses_all_normalized_name_tokens_and_multi_value_facets(store):
    records = [
        ("one", "Testing.Word-Guess_fork.cell-a.000", "word_guess", False),
        ("two", "Testing-Calendar-fork.cell-b.000", "calendar", False),
        ("three", "Unrelated.cell-c.000", "word_guess", True),
    ]
    for uid, episode_id, environment_id, stopped in records:
        trace, manifest = make_trace(uid, run_id=episode_id, environment_id=environment_id)
        trace.stopped = stopped
        store.put_episode(trace, manifest)

    matched, _ = store.episode_summaries({"q": "testing fork"})
    assert {row["episode_uid"] for row in matched} == {"one", "two"}
    punctuated, _ = store.episode_summaries({"q": "testing,fork"})
    assert {row["episode_uid"] for row in punctuated} == {"one", "two"}
    filtered, _ = store.episode_summaries({
        "q": "testing fork",
        "environment_id": ["word_guess", "calendar"],
        "status": ["COMPLETED"],
    })
    assert {row["episode_uid"] for row in filtered} == {"one", "two"}
    only_word_guess, _ = store.episode_summaries({
        "q": "testing fork", "environment_id": ["word_guess"], "status": ["COMPLETED"],
    })
    assert [row["episode_uid"] for row in only_word_guess] == ["one"]

    facets = store.episode_facets()
    assert facets["environments"] == [
        {"value": "calendar", "count": 1}, {"value": "word_guess", "count": 2},
    ]
    assert facets["statuses"] == [
        {"value": "COMPLETED", "count": 2}, {"value": "STOPPED", "count": 1},
    ]


def test_episode_search_cursor_continues_the_same_ordered_result_set(store):
    for index in range(3):
        store.put_episode(*make_trace(
            f"g{index}", run_id=f"Testing.Fork.cell-{index}.000", environment_id="word_guess",
        ))
    conn = store._connect()
    try:
        for index in range(3):
            conn.execute(
                "UPDATE episodes SET created_at = ? WHERE episode_uid = ?",
                (f"2026-01-01T00:00:0{index}Z", f"g{index}"),
            )
        conn.commit()
    finally:
        conn.close()

    first, cursor = store.episode_summaries({"q": "testing fork"}, limit=2)
    second, next_cursor = store.episode_summaries({"q": "testing fork"}, limit=2, cursor=cursor)
    assert [row["episode_uid"] for row in first] == ["g2", "g1"]
    assert [row["episode_uid"] for row in second] == ["g0"]
    assert next_cursor is None


def test_phase_three_schema_migrates_a_pre_token_episode_table_before_indexing(tmp_path):
    """``CREATE TABLE IF NOT EXISTS`` must not index a column it has not added yet."""
    path = tmp_path / "legacy.db"
    legacy_schema = SCHEMA.replace(
        "    -- Lower-cased, punctuation-delimited episode-id tokens. This is a\n"
        "    -- rebuildable search projection; the durable episode identity remains\n"
        "    -- ``episode_id`` and trace config/provenance.\n"
        "    episode_tokens    TEXT NOT NULL DEFAULT '',\n",
        "",
    )
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        conn.executescript(legacy_schema)
        conn.execute(
            "INSERT INTO episodes (episode_uid, episode_id, config, events, final_state, metrics, manifest) "
            "VALUES ('legacy', 'Testing.Word-Guess_fork.cell-a.000', '{}', '[]', '{}', '{}', '{}')"
        )
        apply_schema(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(episodes)")}
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(episodes)")}
        tokens = conn.execute(
            "SELECT episode_tokens FROM episodes WHERE episode_uid = 'legacy'"
        ).fetchone()["episode_tokens"]

    assert "episode_tokens" in columns
    assert "idx_episodes_tokens" in indexes
    assert tokens == "testing word guess fork cell a 000"
