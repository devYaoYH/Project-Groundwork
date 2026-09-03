---
name: a2a-data-exploration
description: Explore and report on A2A experiment episodes stored in a local SQLite database. Use when asked to inspect runs, compare environment metrics, examine messages or events, validate stored episodes, or rebuild the local Calendar leaderboard without cloud services.
---

# A2A Data Exploration

Use the local SQLite trace store as the primary source. Keep analysis
sink-agnostic where practical through `EpisodeDataset`, and state the database
path, filters, and metric definitions in every result.

## Workflow

1. Confirm the database exists and query `episodes` read-only. If using Compose,
   check `GET /api/health` and use `/data/a2a_traces.db` inside its containers.
2. Establish scope with `environment_id`, `experiment_name`, cell labels, and time
   range before computing aggregates. Do not silently combine incompatible
   experiments.
3. Use `EpisodeDataset.from_config({"backend": "sqlite", "path": PATH})` for
   pandas analysis, or parameterised SQL for lightweight aggregation.
4. Inspect the resolved `config` as well as metrics before comparing runs:
   model, prompt variant, temperature, task, seed, and environment-version changes
   can invalidate a comparison.
5. Report sample sizes, excluded stopped/failed episodes, null metric handling,
   and exact queries or code used.

## Common views

```python
from a2a_engine import EpisodeDataset

ds = EpisodeDataset.from_config({"backend": "sqlite", "path": "./results/a2a_traces.db"})
games = ds.to_episodes_df()
messages = ds.to_messages_df()
events = ds.to_events_df()
```

```sql
SELECT environment_id, COUNT(*) AS runs
FROM episodes
WHERE stopped = 0
GROUP BY environment_id;
```

Use `to_messages_df()` only for events with `data.speaker` and `data.text`.
Preserve event-level context by joining it back through `episode_uid` when needed.

## Scores

Treat score meaning as environment-owned. Never rank different games by unrelated raw
metrics. Calendar has the only shipped `RatingEvent` adapter; query
`/api/leaderboards/calendar` or rebuild its OpenSkill snapshot from Calendar
episodes. A new environment joins the board only after it defines its own `MetricSpec`
and `RatingEvent` extraction.
