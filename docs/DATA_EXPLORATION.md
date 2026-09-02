# Data exploration

Start with the SQLite database produced by the local stack. It is the source of
truth for a local experiment and needs no cloud account.

## Browse traces

Run `docker compose up --build`, then open `http://localhost:8080`. The hub
lists all persisted traces and opens each in the shared event viewer. Its local
API is intentionally small:

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | health and trace count |
| `GET /api/traces` | trace metadata and metrics |
| `GET /api/traces/<game_id>` | one complete `GameTraceBase` JSON document |
| `GET /api/traces/<game_id>/artifacts` | digest-bound, post-episode metric artifacts |
| `GET /api/traces/<game_id>/observability` | local JSONL OTel spans correlated by the persisted OTel trace ID |
| `GET /api/leaderboards/calendar` | Calendar's rebuilt OpenSkill snapshot |

## Notebook or script

```python
from a2a_engine import GameDataset

ds = GameDataset.from_config({"backend": "sqlite", "path": "./results/a2a_traces.db"})
games = ds.to_games_df()       # one row per run; metrics_* and final_* columns
messages = ds.to_messages_df() # one row per speaker/text event
events = ds.to_events_df()     # one row per raw event

calendar = ds.filter_by(game_name="calendar")
```

The loader is sink-agnostic, so the same code works for another configured
backend. For a local stack volume, use `/data/a2a_traces.db` from inside a
container or copy the volume's database out before opening it locally.

## SQL

SQLite keeps source traces and derived reporting records separate. `traces` is
the immutable game record; `derived_artifacts`, `rating_events`, and
`rating_snapshots` are replayable outputs. `config`, `events`, `final_state`,
`metrics`, `observability`, `environment`, `episode`, and `manifest` are JSON
text columns. `environment` and `episode` are present for typed runs and
identify the portable world plus the exact expanded episode.

```sql
SELECT game_name,
       COUNT(*) AS runs,
       AVG(json_extract(metrics, '$.efficiency')) AS mean_efficiency
FROM traces
GROUP BY game_name;
```

Use a SQLite client or `sqlite3 ./results/a2a_traces.db`; no application
service is required for querying.

## Post-episode metrics

The game loop writes native metrics at completion. A metric declared as
`producer: derived` is deliberately computed later from the immutable trace,
then stored as a digest-bound artifact rather than altering that trace. This
keeps live execution independent from reporting and makes backfills safe:

```bash
uv run python scripts/materialize_metrics.py \
  --database ./results/a2a_traces.db --game calendar
```

Run the command again to confirm idempotence; unchanged artifacts report
`changed=0`. The trace page shows the resulting artifact payload under
**Derived metrics**, and the API exposes the same payload at `/artifacts`.

## Scores and leaderboards

Scores are game-defined. A game that wants to join a central OpenSkill board
must export a `RatingEvent` adapter and its `MetricSpec`s, as Calendar does in
`calendar_game.ratings`. The local control plane rebuilds Calendar's board on
demand from completed traces in SQLite. It never accepts a live-session rating
write. Missing post-hoc measurements (such as Calendar VPS) suppress that
metric rather than treating it as zero.

To attach a completed Calendar VPS analysis, use the ingestion command. It
verifies each result against the stored trace digest and can rebuild the board
afterward:

```bash
uv run python games/calendar/analysis/scripts/ingest_vps_artifacts.py \
  analysis/outputs/reflection_vps_metric/game_target_summary.csv \
  --database ./results/a2a_traces.db --rebuild
```

The repository ships a reusable exploration skill at
`skills/a2a-data-exploration/SKILL.md`. Add that directory to Codex's configured
skills location to invoke it as `$a2a-data-exploration`.
