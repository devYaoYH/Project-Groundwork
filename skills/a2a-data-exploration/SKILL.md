---
name: a2a-data-exploration
description: Explore and report on A2A experiment traces stored in a local SQLite database. Use when asked to inspect runs, compare game metrics, examine messages or events, validate stored traces, or rebuild the local Calendar leaderboard without cloud services.
---

# A2A Data Exploration

Use the local SQLite trace store as the primary source. Keep analysis
sink-agnostic where practical through `GameDataset`, and state the database
path, filters, and metric definitions in every result.

## Workflow

1. Confirm the database exists and query `traces` read-only. If using Compose,
   check `GET /api/health` and use `/data/a2a_traces.db` inside its containers.
2. Establish scope with `game_name`, `experiment_name`, batch labels, and time
   range before computing aggregates. Do not silently combine incompatible
   experiments.
3. Use `GameDataset.from_config({"backend": "sqlite", "path": PATH})` for
   pandas analysis, or parameterised SQL for lightweight aggregation.
4. Inspect the resolved `config` as well as metrics before comparing runs:
   model, prompt variant, temperature, task, seed, and game-version changes
   can invalidate a comparison.
5. Report sample sizes, excluded stopped/failed traces, null metric handling,
   and exact queries or code used.

## Common views

```python
from a2a_engine import GameDataset

ds = GameDataset.from_config({"backend": "sqlite", "path": "./results/a2a_traces.db"})
games = ds.to_games_df()
messages = ds.to_messages_df()
events = ds.to_events_df()
```

```sql
SELECT game_name, COUNT(*) AS runs
FROM traces
WHERE stopped = 0
GROUP BY game_name;
```

Use `to_messages_df()` only for events with `data.speaker` and `data.text`.
Preserve event-level context by joining it back through `game_id` when needed.

## Scores

Treat score meaning as game-owned. Never rank different games by unrelated raw
metrics. Calendar has the only shipped `RatingEvent` adapter; query
`/api/leaderboards/calendar` or rebuild its OpenSkill snapshot from Calendar
traces. A new game joins the board only after it defines its own `MetricSpec`
and `RatingEvent` extraction.
