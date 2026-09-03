# Local collaboration stack

The Compose stack is the supported cloud-free onboarding path. It runs all
packaged games, writes episodes to a named-volume SQLite database, and hosts one
central browser for episodes and the Calendar leaderboard. It uses no AWS, GCP,
Firestore, S3, or model API credentials.

## Start

```bash
docker compose up --build
```

The `runner` service completes the deterministic four-environment smoke suite, while
`viewer` remains at `http://localhost:8080`. The shared database is the
`a2a-data` volume and survives restarts.

```bash
docker compose ps
docker compose logs runner
docker compose down                 # preserve data
docker compose down -v              # delete the local database deliberately
```

## Run another local job

Keep `--storage-path /data/a2a.db` so every job publishes to the same
local viewer. The command below still has no provider calls because it is a
smoke test.

```bash
docker compose run --rm runner \
  a2a-run games/buyer-seller/experiments/example.yaml \
  --smoke-test --storage-path /data/a2a.db
```

For a real model run, pass a local `.env` file with `--env-file`; credentials
are not included in the image or Compose file. A local OpenAI-compatible model
server can be reached from Docker Desktop through
`http://host.docker.internal:<port>/v1`; configure that endpoint in a private
experiment YAML or release-specific overlay.

## Launch experiments from the browser

`http://localhost:8080/control.html` is the local control plane. It lists the
installed environment releases, registers a checked-in experiment YAML, launches a
launch, streams the runner's events, and links each episode to its persisted
trace and its Redis replay. The browser never uploads Python, a Dockerfile, or
an image: it only selects a release that is already installed and a YAML that
is already in the workspace.

```bash
curl -X POST http://localhost:8080/api/experiments \
  -H 'Content-Type: application/json' \
  -d '{"release_id":"calendar","yaml_path":"games/calendar/experiments/typed_local_smoke.yaml"}'

curl -X POST http://localhost:8080/api/launches \
  -H 'Content-Type: application/json' \
  -d '{"experiment_id":"<id>","max_parallelism":1,"smoke_test":true}'
```

A smoke launch runs one episode per cell rather than each cell's declared
`count`, and the control plane queues exactly those episodes so no attempt is
recorded for a run that never happened.

Episode attempts carry a terminal status:

| Status | Meaning |
|---|---|
| `COMPLETED` | the episode exists in `episodes` for this attempt's deterministic `episode_id` |
| `FAILED` | the runner reported this episode as failed; the attempt keeps its own diagnostic |
| `CANCELLED` | the launch was cancelled before this episode reported |
| `UNREPORTED` | no episode exists for this id and the launch has ended — a gap to investigate, not a result |

`attempt` is monotonic per `episode_id` across launches, so re-running an
episode adds a row rather than replacing one. A failed attempt is kept, not
tombstoned: "the result for this episode" is a query — the completed attempt
with the highest `attempt` — rather than a stored flag.

## Progress, and what happens after a restart

Progress is an identity join, not a log parse. The whole planned episode set is
written at launch time and `episode_id` is deterministic, so
`GET /api/launches/<id>` answers `progress.by_cell` from
`attempts LEFT JOIN episodes` — the same question `--resume` asks the store.
Runner stdout still feeds `launch_events` so a live launch feels responsive,
but it is never the source of truth.

Because that join needs no live process, a restart resolves what it stranded.
Constructing the control plane reconciles every launch left `QUEUED`,
`RUNNING`, or `CANCELLING`: each attempt is resolved from the join, and one
with no episode is looked for in the durable event log before being written
off. A recovered episode is stored with `status = PARTIAL` — evidence for
inspection and retry, never a successful result, and never counted as progress.

## The durable event log

Every episode appends its events to
`<results-dir>/<experiment>/<episode_uid>.events.jsonl`, flushed per event,
before anything else sees them. Redis, when configured, is a second copy rather
than the only incremental one, so an episode killed mid-run is recoverable with
no Redis in the picture. Each line carries the same envelope a Redis stream
entry does, so one projection reads either source.

## Components and boundary

| Component | Local implementation | Later cloud replacement |
|---|---|---|
| experiment execution | `runner` container / `a2a-run` | Kubernetes Job or worker deployment |
| trace egress | SQLite named volume | S3, Firestore, or a shared database adapter |
| viewer and control plane | standard-library `local_stack/server.py` + static viewer, including local OTel span correlation | a hosted API serving the same contracts |
| leaderboard | Calendar snapshot replayed from completed SQLite episodes | shared artifact/materialization store |

The image is suitable for a Kubernetes Job: it needs an experiment YAML,
release variables, and writable storage. SQLite is deliberately
single-host/single-volume; do not mount one SQLite file read-write from multiple
nodes. For a cluster, retain the same runner image and replace only the storage
configuration with a shared backend when that integration is ready.

## API and data contract

The local service exposes read-only trace endpoints — `/api/health`,
`/api/episodes`, `/api/episodes/<episode_uid>`, `/api/episodes/<episode_uid>/artifacts`,
`/api/episodes/<episode_uid>/observability`, and `/api/leaderboards/calendar` —
alongside the control-plane endpoints `/api/releases`, `/api/environments`,
`/api/experiments`, `/api/launches`, `/api/launches/<id>`,
`/api/launches/<id>/events` (SSE), `/api/launches/<id>/cancel`, and
`/api/streams/<stream>`. The trace corpus itself stays read-only: the runner
writes source episodes and post-hoc analysis can add only digest-bound derived
artifacts.

`GET /api/episodes` is paginated and filtered server-side. It accepts
`experiment_id`, `experiment_name`, `environment_id`, `cell_id`, `episode_id`,
`release_id`, `item_id`, `status`, `limit` and `cursor`; filters are pushed
into SQL against promoted, indexed columns, and an unrecognised key is dropped
rather than interpolated. The response is
`{episodes: [...], next_cursor, filters}`.

**One database.** Control-plane records and the episode fact table live in the
same SQLite file (`--database`; there is no `--control-database`), so a fact row
joins its dimensions in real SQL and no launch is linked to a trace by a string
parsed out of a log line:

```bash
sqlite3 /data/a2a.db "select cell_id, count(*) from episodes group by 1"
```

`/api/launches/<id>/events` sends unnamed SSE frames whose payload carries the
event `kind`, so a client never has to enumerate event kinds in advance and a
newly added kind cannot be silently dropped. Rating events and snapshots are rebuilt from
completed episodes, never written alongside a running environment. See
`docs/DATA_EXPLORATION.md` for analysis and `docs/AGENT_CONFIGURATION.md` for
model configuration. Compose records the full local OTel projection in the
same named volume (`otel-spans.jsonl`) and the viewer joins it to a trace by
the persisted OTel trace ID. SQLite remains the canonical replay record.
