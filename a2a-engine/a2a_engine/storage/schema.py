"""Every table of the single local database, in one module.

The trace corpus and the control plane used to live in two SQLite files that
could never be joined: ``PRAGMA foreign_keys`` does not cross files, and the
only link was a ``trace_uri`` string recovered by regex from the runner's
stdout.  They are now one database with a star schema, so a fact row reaches
its dimensions in SQL.

    FACT   episodes        episode_uid PK - episode_id - attempt - experiment_id
                           release_id - item_id - seed - status - timestamps
                           config/events/final_state/metrics/observability/manifest (JSON)

    DIM    releases        id PK - environment_id - version - declaration_sha256
                           item_bank_sha256 - oracle_version
    DIM    items           item_id PK - item_bank_sha256 - params - oracle_result
    DIM    experiments     id PK - name - release_id - created_at
    LOG    launches        id PK - experiment_id - status - timestamps
    LOG    launch_events   id - launch_id - kind - payload - created_at

Ownership stays legible by table name: the episode store owns ``episodes`` and
the analysis tables hanging off it, the control plane owns ``experiments`` /
``launch*`` / ``attempts``.  That maps onto Postgres schemas later without a
redesign.

**Postgres portability is a discipline, not a component.**  Only ``TEXT``,
``INTEGER`` and ``REAL`` appear; JSON is stored in ``TEXT`` columns that become
``JSONB`` cleanly; there is no ``AUTOINCREMENT``; and dimensions are declared
before the facts that reference them so the script also loads on a database
that resolves foreign keys at DDL time.

Both the episode store and the control plane execute the whole script, so a
database created by either is complete: ``a2a-run --storage-path fresh.db``
produces a file the control plane can open, and vice versa.
"""

from __future__ import annotations

SCHEMA = """
-- DIMENSIONS ---------------------------------------------------------------

-- A release is a version of an environment: pinned code, its declaration, its
-- oracle and its item bank as one selectable unit.  Identity is the release
-- id; comparability is ``oracle_version`` and ``item_bank_sha256``, which are
-- recorded separately so releases sharing bank bytes pool automatically.
CREATE TABLE IF NOT EXISTS releases (
    id                TEXT PRIMARY KEY,
    environment_id    TEXT NOT NULL,
    version           TEXT,
    declaration_sha256 TEXT,
    item_bank_sha256  TEXT,
    oracle_version    TEXT,
    package           TEXT,
    source_ref        TEXT NOT NULL DEFAULT '',
    metadata          TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_releases_environment ON releases(environment_id);

-- One scenario instance from a frozen bank.  Populated when a design draws
-- items; the fact table's ``item_id`` is null until then.
CREATE TABLE IF NOT EXISTS items (
    item_id           TEXT PRIMARY KEY,
    item_bank_sha256  TEXT NOT NULL,
    params            TEXT NOT NULL DEFAULT '{}',
    oracle_result     TEXT NOT NULL DEFAULT 'null',
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_items_bank ON items(item_bank_sha256);

CREATE TABLE IF NOT EXISTS experiments (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    environment_id    TEXT NOT NULL,
    release_id        TEXT NOT NULL REFERENCES releases(id),
    yaml_path         TEXT NOT NULL,
    config_sha256     TEXT NOT NULL,
    -- Design experiments retain their authored text verbatim.  Legacy YAML
    -- experiments leave these null and remain launchable as direct inputs.
    design_text       TEXT,
    design_sha256     TEXT,
    locked_at         TEXT,
    forked_from       TEXT REFERENCES experiments(id),
    -- Compatibility normalization records the original content when a
    -- narrowly-scoped metadata correction creates a current design revision.
    -- Execution plans and traces retain their original provenance bytes.
    authored_design_text TEXT,
    authored_design_sha256 TEXT,
    authored_config_sha256 TEXT,
    role_normalization TEXT,
    created_at        TEXT NOT NULL
);

-- The fully expanded design plan.  ``episode_configs`` keeps the exact
-- disposable execution configs produced at lock time; ``levels`` remains the
-- compact, queryable description of the cell.
CREATE TABLE IF NOT EXISTS cells (
    cell_id           TEXT PRIMARY KEY,
    experiment_id     TEXT NOT NULL REFERENCES experiments(id),
    levels            TEXT NOT NULL,
    episodes_planned  INTEGER NOT NULL,
    episode_configs   TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cells_experiment ON cells(experiment_id, cell_id);

CREATE TABLE IF NOT EXISTS participants (
    participant_id    TEXT NOT NULL,
    experiment_id     TEXT NOT NULL REFERENCES experiments(id),
    kind              TEXT NOT NULL,
    binding           TEXT,
    role              TEXT,
    config_sha256     TEXT NOT NULL,
    PRIMARY KEY (participant_id, experiment_id)
);
CREATE INDEX IF NOT EXISTS idx_participants_experiment ON participants(experiment_id);

-- FACT ---------------------------------------------------------------------

-- Grain: one attempt.  ``episode_id`` is deterministic and stable across
-- attempts; ``episode_uid`` is unique per attempt.  The promoted columns are a
-- query-performance decision on a rebuildable projection -- the durable copy
-- is the provenance block inside ``config``, which travels with the trace.
CREATE TABLE IF NOT EXISTS episodes (
    episode_uid       TEXT PRIMARY KEY,
    environment_id    TEXT,
    experiment_name   TEXT,
    episode_id        TEXT,
    -- Lower-cased, punctuation-delimited episode-id tokens. This is a
    -- rebuildable search projection; the durable episode identity remains
    -- ``episode_id`` and trace config/provenance.
    episode_tokens    TEXT NOT NULL DEFAULT '',
    cell_id           TEXT,
    episode_idx       INTEGER,
    experiment_id     TEXT REFERENCES experiments(id),
    release_id        TEXT REFERENCES releases(id),
    item_id           TEXT REFERENCES items(item_id),
    attempt           INTEGER NOT NULL DEFAULT 1,
    seed              INTEGER,
    -- COMPLETED | STOPPED | PARTIAL.  A PARTIAL row is a trace recovered from
    -- an interrupted episode's event log: evidence, not a result.
    status            TEXT NOT NULL DEFAULT 'COMPLETED',
    config            TEXT NOT NULL,
    events            TEXT NOT NULL,
    final_state       TEXT NOT NULL,
    metrics           TEXT NOT NULL,
    release           TEXT NOT NULL DEFAULT '{}',
    episode           TEXT NOT NULL DEFAULT '{}',
    observability     TEXT NOT NULL DEFAULT '{}',
    started_at        TEXT,
    ended_at          TEXT,
    stopped           INTEGER DEFAULT 0,
    manifest          TEXT NOT NULL,
    created_at        TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_episodes_experiment ON episodes(experiment_name);
CREATE INDEX IF NOT EXISTS idx_episodes_environment ON episodes(environment_id);
CREATE INDEX IF NOT EXISTS idx_episodes_episode_id ON episodes(episode_id);
CREATE INDEX IF NOT EXISTS idx_episodes_cell ON episodes(experiment_id, cell_id);
CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);
-- Not unique: the attempt number is assigned by the control plane at plan
-- time, and a runner invoked directly has no control plane to ask.  A unique
-- constraint over a value the writer cannot yet know would turn a legitimate
-- re-run into a silent INSERT OR REPLACE, erasing the earlier attempt.
CREATE INDEX IF NOT EXISTS idx_episodes_attempt ON episodes(episode_id, attempt);

-- CONTROL PLANE ------------------------------------------------------------

CREATE TABLE IF NOT EXISTS launches (
    id                TEXT PRIMARY KEY,
    experiment_id     TEXT NOT NULL REFERENCES experiments(id),
    status            TEXT NOT NULL,
    max_parallelism   INTEGER NOT NULL,
    trace_database    TEXT NOT NULL,
    mode              TEXT NOT NULL DEFAULT 'live',
    execution_path    TEXT,
    shard_index       INTEGER,
    shard_count       INTEGER,
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    ended_at          TEXT,
    error             TEXT
);

-- ``attempt`` is monotonic per ``episode_id`` across launches, which is what
-- replaced UNIQUE(launch_id, episode_id): re-attempting an episode is normal,
-- and a failed attempt is kept rather than tombstoned because it is
-- diagnostically valuable.
CREATE TABLE IF NOT EXISTS attempts (
    id                TEXT PRIMARY KEY,
    launch_id         TEXT NOT NULL REFERENCES launches(id),
    episode_id        TEXT NOT NULL,
    cell_id           TEXT NOT NULL,
    episode_idx       INTEGER NOT NULL,
    attempt           INTEGER NOT NULL DEFAULT 1,
    status            TEXT NOT NULL,
    episode_uri       TEXT,
    error             TEXT,
    started_at        TEXT,
    ended_at          TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_attempt_unique ON attempts(episode_id, attempt);
CREATE INDEX IF NOT EXISTS idx_attempt_launch ON attempts(launch_id);
CREATE INDEX IF NOT EXISTS idx_attempts_cell_execution
    ON attempts(cell_id, episode_id, attempt DESC);

CREATE TABLE IF NOT EXISTS launch_events (
    id                INTEGER PRIMARY KEY,
    launch_id         TEXT NOT NULL REFERENCES launches(id),
    kind              TEXT NOT NULL,
    payload           TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_launch_events_launch ON launch_events(launch_id, id);

-- ANALYSIS PROJECTIONS -----------------------------------------------------

-- Derived data is intentionally separate from the immutable episode trace.
-- Both tables are keyed by episode plus extractor version, so a new analysis
-- can be backfilled without mutating the source record or double-counting.
CREATE TABLE IF NOT EXISTS derived_artifacts (
    episode_uid       TEXT NOT NULL,
    kind              TEXT NOT NULL,
    version           TEXT NOT NULL,
    trace_digest      TEXT NOT NULL,
    payload           TEXT NOT NULL,
    metadata          TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (episode_uid, kind, version)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_episode ON derived_artifacts(episode_uid);

CREATE TABLE IF NOT EXISTS rating_events (
    episode_uid       TEXT NOT NULL,
    environment_id    TEXT NOT NULL,
    adapter_version   TEXT NOT NULL,
    trace_digest      TEXT NOT NULL,
    event             TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (episode_uid, adapter_version)
);
CREATE INDEX IF NOT EXISTS idx_rating_events_environment
    ON rating_events(environment_id, adapter_version);

CREATE TABLE IF NOT EXISTS rating_snapshots (
    environment_id    TEXT NOT NULL,
    adapter_version   TEXT NOT NULL,
    snapshot          TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (environment_id, adapter_version)
);
"""

# ``CREATE TABLE IF NOT EXISTS`` does not migrate a pre-existing local corpus.
# Additive column migrations keep an older database readable rather than
# demanding it be thrown away; each entry is (table, column, ALTER statement).
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("episodes", "observability",
     "ALTER TABLE episodes ADD COLUMN observability TEXT NOT NULL DEFAULT '{}'"),
    ("episodes", "release",
     "ALTER TABLE episodes ADD COLUMN release TEXT NOT NULL DEFAULT '{}'"),
    ("episodes", "episode",
     "ALTER TABLE episodes ADD COLUMN episode TEXT NOT NULL DEFAULT '{}'"),
    ("episodes", "experiment_id", "ALTER TABLE episodes ADD COLUMN experiment_id TEXT"),
    ("episodes", "release_id", "ALTER TABLE episodes ADD COLUMN release_id TEXT"),
    ("episodes", "item_id", "ALTER TABLE episodes ADD COLUMN item_id TEXT"),
    ("episodes", "attempt",
     "ALTER TABLE episodes ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1"),
    ("episodes", "episode_tokens",
     "ALTER TABLE episodes ADD COLUMN episode_tokens TEXT NOT NULL DEFAULT ''"),
    ("episodes", "seed", "ALTER TABLE episodes ADD COLUMN seed INTEGER"),
    ("episodes", "status",
     "ALTER TABLE episodes ADD COLUMN status TEXT NOT NULL DEFAULT 'COMPLETED'"),
    ("releases", "version", "ALTER TABLE releases ADD COLUMN version TEXT"),
    ("releases", "declaration_sha256",
     "ALTER TABLE releases ADD COLUMN declaration_sha256 TEXT"),
    ("releases", "item_bank_sha256", "ALTER TABLE releases ADD COLUMN item_bank_sha256 TEXT"),
    ("releases", "oracle_version", "ALTER TABLE releases ADD COLUMN oracle_version TEXT"),
    ("experiments", "environment_id", "ALTER TABLE experiments ADD COLUMN environment_id TEXT"),
    ("experiments", "design_text", "ALTER TABLE experiments ADD COLUMN design_text TEXT"),
    ("experiments", "design_sha256", "ALTER TABLE experiments ADD COLUMN design_sha256 TEXT"),
    ("experiments", "locked_at", "ALTER TABLE experiments ADD COLUMN locked_at TEXT"),
    ("experiments", "forked_from", "ALTER TABLE experiments ADD COLUMN forked_from TEXT"),
    ("experiments", "authored_design_text", "ALTER TABLE experiments ADD COLUMN authored_design_text TEXT"),
    ("experiments", "authored_design_sha256", "ALTER TABLE experiments ADD COLUMN authored_design_sha256 TEXT"),
    ("experiments", "authored_config_sha256", "ALTER TABLE experiments ADD COLUMN authored_config_sha256 TEXT"),
    ("experiments", "role_normalization", "ALTER TABLE experiments ADD COLUMN role_normalization TEXT"),
    ("participants", "role", "ALTER TABLE participants ADD COLUMN role TEXT"),
    ("attempts", "attempt", "ALTER TABLE attempts ADD COLUMN attempt INTEGER NOT NULL DEFAULT 1"),
    ("launches", "mode", "ALTER TABLE launches ADD COLUMN mode TEXT NOT NULL DEFAULT 'live'"),
    ("launches", "execution_path", "ALTER TABLE launches ADD COLUMN execution_path TEXT"),
    ("launches", "shard_index", "ALTER TABLE launches ADD COLUMN shard_index INTEGER"),
    ("launches", "shard_count", "ALTER TABLE launches ADD COLUMN shard_count INTEGER"),
)


def apply_schema(conn) -> None:
    """Create every table and apply the additive column migrations.

    Safe to call from either owner and from several connections at once: every
    statement is ``IF NOT EXISTS`` and each migration is guarded by an actual
    column check rather than by a version number nobody maintains.
    """
    # Schema DDL can safely reference the columns a fresh table declares, but
    # an ``IF NOT EXISTS`` table leaves an older table unchanged. Keep indexes
    # for newly migrated columns out of this script until their migration has
    # run below; otherwise SQLite rejects the whole schema application before
    # it reaches the additive ALTER.
    conn.executescript(SCHEMA)
    existing: dict[str, set[str]] = {}
    for table, column, statement in MIGRATIONS:
        if table not in existing:
            existing[table] = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if existing[table] and column not in existing[table]:
            conn.execute(statement)
            existing[table].add(column)
    # ``episode_tokens`` is only a read projection, so old traces retain every
    # durable byte while becoming searchable after an additive migration.
    if "episodes" in existing and "episode_tokens" in existing["episodes"]:
        from re import sub

        rows = conn.execute(
            "SELECT episode_uid, episode_id FROM episodes WHERE episode_tokens = ''"
        ).fetchall()
        conn.executemany(
            "UPDATE episodes SET episode_tokens = ? WHERE episode_uid = ?",
            [
                (" ".join(token for token in sub(r"[^0-9A-Za-z]+", " ", row["episode_id"] or "").lower().split()),
                 row["episode_uid"])
                for row in rows
            ],
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_episodes_tokens ON episodes(episode_tokens)")
    conn.commit()
