"""Physical execution identity for databases written before shard fan-out."""

import sqlite3

from a2a_engine.storage.schema import apply_schema


def test_legacy_duplicate_attempt_rows_gain_distinct_execution_counters(tmp_path):
    db = sqlite3.connect(tmp_path / "legacy.db")
    db.row_factory = sqlite3.Row
    apply_schema(db)
    db.execute("DROP INDEX idx_episodes_physical")
    db.execute("ALTER TABLE episodes DROP COLUMN execution")
    db.execute("ALTER TABLE episodes DROP COLUMN shard_index")
    for uid, created in (("older", "2025-01-01"), ("newer", "2025-01-02")):
        db.execute(
            "INSERT INTO episodes (episode_uid, episode_id, attempt, created_at, "
            "config, events, final_state, metrics, manifest) VALUES (?,?,?,?,?,?,?,?,?)",
            (uid, "shared-slot", 1, created, "{}", "[]", "{}", "{}", "{}"),
        )
    db.commit()

    apply_schema(db)
    rows = db.execute(
        "SELECT episode_uid, execution FROM episodes ORDER BY created_at"
    ).fetchall()
    assert [(row["episode_uid"], row["execution"]) for row in rows] == [
        ("older", 0), ("newer", 1),
    ]
    assert db.execute(
        "SELECT name FROM sqlite_master WHERE name = 'idx_episodes_physical'"
    ).fetchone() is not None
    apply_schema(db)
    assert db.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 2
    db.close()
