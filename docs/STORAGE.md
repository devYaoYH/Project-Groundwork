# Storage: where your traces go

Every run produces a `GameTraceBase` (the record of what happened) and a
`RunManifest` (what would be needed to reproduce it). Both go to a **sink** you
choose per experiment. Nothing about a game's code changes when you switch sinks.

## The four backends

| backend | what it is | setup | good for |
|---|---|---|---|
| `sqlite` | one database file | none | **start here.** Queryable, single file, no account |
| `local` | JSON tree under `results/` | none | debugging one run — every trace is a readable file |
| `s3` | S3 bucket, mirrored from local JSON | AWS CLI + profile | sharing a corpus across a lab |
| `firestore` | Firestore collection | `google-cloud-firestore` + ADC | the existing negotiation corpus |

`sqlite` and `local` are the ground truth when selected. `s3` and `firestore`
write locally *first* and mirror best-effort — a remote failure is recorded in
the manifest and never fails a run, so a network blip cannot cost you a batch of
model spend.

## Choosing one

Name a sink in the experiment's `storage:` block:

```yaml
storage:
  backend: sqlite
  path: ./results/my_study.db
```

Better, reference a shared named sink so the details live in one place:

```yaml
# experiments/storage.yaml
sinks:
  local_db:
    backend: sqlite
    path: ${A2A_TRACE_DB:-./results/a2a_traces.db}
  lab_s3:
    backend: s3
    bucket: ${A2A_TRACE_BUCKET}
    prefix: ${A2A_TRACE_PREFIX:-traces}
```

```yaml
# experiments/my_study.yaml
storage:
  extends: local_db          # or: storage: local_db
  path: ./results/pilot.db   # inline keys override the sink
```

A `storage.yaml` sitting next to your experiment file is picked up
automatically. Point elsewhere with `sinks_path:`.

### Inheritance and precedence

Lowest to highest:

1. the game's registered default (`register_game(..., storage={...})`)
2. the named sink referenced by `storage.extends`
3. inline keys under `storage:`
4. CLI flags (`--storage-backend`, `--storage-path`, `--s3-bucket`)

Sinks themselves support single inheritance via `extends:`, resolved in one
forward pass — a sink may only extend one defined earlier in the file, which
makes cycles impossible to write.

One deliberate rule: **if the resolved backend differs from the game's
registered default, the game default is dropped rather than merged.** Pointing
a game at SQLite should not drag a remote `prefix` along into the manifest.

Storage is resolved per *experiment*, not per batch — the runner holds one open
sink for the whole run. An experiment spanning several games (see
`experiments/all_games_smoke.yaml`) therefore writes them all to one place,
which is what makes cross-game comparison a single query.

### Secrets stay in `.env`

Values interpolate `${VAR}` and `${VAR:-fallback}` from the environment, so
committed YAML names a bucket variable rather than a bucket:

```yaml
bucket: ${A2A_TRACE_BUCKET}      # unset -> hard error, with the variable named
prefix: ${A2A_TRACE_PREFIX:-traces}   # unset -> "traces"
profile: ${AWS_PROFILE:-}        # unset -> "" (explicitly optional)
```

A bare `${VAR}` that is unset raises rather than silently passing the literal
string `"${VAR}"` to a cloud SDK, which is the kind of error that otherwise
surfaces as a confusing 403 an hour later.

## Checking it works before you spend money

```bash
a2a-run experiments/my_study.yaml --smoke-test
```

Three gates, so a failure tells you which layer broke:

1. **Sink reachable** — `store.check()`. SQLite and local do a full
   write/read/delete canary; S3 and Firestore probe read access, deliberately
   leaving no debris in a shared bucket or collection.
2. **Batches expand** — presets, sinks and `resolve_config` all resolve, and
   every game a batch names is installed.
3. **Round-trip** — one run per batch with the game's scripted agents, persisted
   through the sink and **read back**. A sink that accepts writes and stores
   nothing fails here rather than reporting success.

No API keys, no model spend. Compare with `--dry-run`, which answers the other
question — *are my models reachable?* — by checking keys and persisting nothing.

## Reading results back

The loader is sink-agnostic, so analysis code does not change when storage does:

```python
from a2a_engine import GameDataset

ds = GameDataset.from_config({"backend": "sqlite", "path": "./results/a2a.db"})

games    = ds.to_games_df()      # one row per run, metrics_* and final_* columns
messages = ds.to_messages_df()   # one row per utterance
events   = ds.to_events_df()     # one row per event

buyers = ds.filter_by(game_name="buyer_seller")
```

Filters are pushed into the backend where it can use them — SQLite narrows the
query rather than loading everything and filtering in Python:

```python
ds = GameDataset.from_config(storage, filters={"game_name": "buyer_seller"})
```

To reuse the exact block an experiment ran with:

```python
from a2a_engine import GameDataset, load_experiment, resolve_storage

spec = load_experiment("experiments/my_study.yaml")
ds = GameDataset.from_config(resolve_storage(spec))
```

Because `sqlite` is a plain database, anything that speaks SQL works too:

```sql
SELECT game_name, COUNT(*), AVG(json_extract(metrics, '$.efficiency'))
FROM traces GROUP BY game_name;
```

## Adding a backend

Implement `put_trace` / `get_trace` / `list_traces` / `check`, then register:

```python
from a2a_engine.storage import register_store

class PostgresTraceStore:
    name = "postgres"
    def __init__(self, *, dsn: str, results_dir="./results", **_ignored): ...
    def put_trace(self, trace, manifest) -> str: ...
    def get_trace(self, game_id): ...
    def list_traces(self, filters=None, limit=50, cursor=None): ...
    def check(self) -> StoreCheck: ...

register_store("postgres", PostgresTraceStore)
```

Optional extras the runner uses when present: `completed_run_ids(experiment_name)`
powers `--resume`, and `iter_traces(filters)` gives `GameDataset` a streaming
path instead of paging `list_traces`.

Accept `**_ignored` in `__init__` — sinks carry keys like `sink:` that identify
the config, not the connection.
