# Data exploration

Start with the SQLite database produced by the local stack. It is the source of
truth for a local experiment and needs no cloud account.

## Browse episodes

Run `docker compose up --build`, then open `http://localhost:8080`. The hub
lists all persisted episodes and opens each in the shared event viewer. Its local
API is intentionally small:

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | health and trace count |
| `GET /api/episodes` | trace metadata and metrics |
| `GET /api/episodes/<episode_uid>` | one complete `EpisodeTrace` JSON document |
| `GET /api/episodes/<episode_uid>/artifacts` | digest-bound, post-episode metric artifacts |
| `GET /api/episodes/<episode_uid>/observability` | local JSONL OTel spans correlated by the persisted OTel trace ID |
| `GET /api/leaderboards/calendar` | Calendar's rebuilt OpenSkill snapshot |

## Notebook or script

```python
from a2a_engine import EpisodeDataset

ds = EpisodeDataset.from_config({"backend": "sqlite", "path": "./results/a2a.db"})
games = ds.to_episodes_df()       # one row per run; metrics_* and final_* columns
messages = ds.to_messages_df() # one row per speaker/text event
events = ds.to_events_df()     # one row per raw event

calendar = ds.filter_by(environment_id="calendar")
```

The loader is sink-agnostic, so the same code works for another configured
backend. For a local stack volume, use `/data/a2a.db` from inside a
container or copy the volume's database out before opening it locally.

## SQL

SQLite keeps source episodes and derived reporting records separate. `episodes` is
the immutable environment record; `derived_artifacts`, `rating_events`, and
`rating_snapshots` are replayable outputs. `config`, `events`, `final_state`,
`metrics`, `observability`, `release`, `episode`, and `manifest` are JSON
text columns. `release` and `episode` are present for typed runs and
identify the portable world plus the exact expanded episode.

```sql
SELECT environment_id,
       COUNT(*) AS runs,
       AVG(json_extract(metrics, '$.efficiency')) AS mean_efficiency
FROM episodes
GROUP BY environment_id;
```

Use a SQLite client or `sqlite3 ./results/a2a.db`; no application
service is required for querying.

## Post-episode metrics

The environment loop writes native metrics at completion. A metric declared as
`producer: derived` is deliberately computed later from the immutable trace,
then stored as a digest-bound artifact rather than altering that trace. This
keeps live execution independent from reporting and makes backfills safe:

```bash
uv run python scripts/materialize_metrics.py \
  --database ./results/a2a.db --environment calendar
```

Run the command again to confirm idempotence; unchanged artifacts report
`changed=0`. The trace page shows the resulting artifact payload under
**Derived metrics**, and the API exposes the same payload at `/artifacts`.

## Scores and leaderboards

Scores are environment-defined. A environment that wants to join a central OpenSkill board
must export a `RatingEvent` adapter and its `MetricSpec`s, as Calendar does in
`calendar_game.ratings`. The local control plane rebuilds Calendar's board on
demand from completed episodes in SQLite. It never accepts a live-session rating
write. Missing post-hoc measurements (such as Calendar VPS) suppress that
metric rather than treating it as zero.

To attach a completed Calendar VPS analysis, use the ingestion command. It
verifies each result against the stored trace digest and can rebuild the board
afterward:

```bash
uv run python games/calendar/analysis/scripts/ingest_vps_artifacts.py \
  analysis/outputs/reflection_vps_metric/game_target_summary.csv \
  --database ./results/a2a.db --rebuild
```

The repository ships a reusable exploration skill at
`skills/a2a-data-exploration/SKILL.md`. Add that directory to Codex's configured
skills location to invoke it as `$a2a-data-exploration`.
