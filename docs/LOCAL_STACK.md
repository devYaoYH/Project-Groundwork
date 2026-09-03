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

Keep `--storage-path /data/a2a_traces.db` so every job publishes to the same
local viewer. The command below still has no provider calls because it is a
smoke test.

```bash
docker compose run --rm runner \
  a2a-run games/buyer-seller/experiments/example.yaml \
  --smoke-test --storage-path /data/a2a_traces.db
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
  -d '{"release_id":"local-calendar","yaml_path":"games/calendar/experiments/typed_local_smoke.yaml"}'

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
| `COMPLETED` | the runner reported this episode with its trace URI |
| `FAILED` | the runner reported this episode as failed; the attempt keeps its own diagnostic |
| `CANCELLED` | the launch was cancelled before this episode reported |
| `UNREPORTED` | the runner exited successfully but never reported this episode — a gap to investigate, not a result |

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
alongside the control-plane endpoints `/api/releases`, `/api/experiments`,
`/api/launches`, `/api/launches/<id>`, `/api/launches/<id>/events` (SSE),
`/api/launches/<id>/cancel`, and `/api/streams/<stream>`. The trace corpus
itself stays read-only: the runner writes source episodes and post-hoc analysis
can add only digest-bound derived artifacts. Control-plane records live in a
separate SQLite database and reference episodes by URI rather than restating
them.

`/api/launches/<id>/events` sends unnamed SSE frames whose payload carries the
event `kind`, so a client never has to enumerate event kinds in advance and a
newly added kind cannot be silently dropped. Rating events and snapshots are rebuilt from
completed episodes, never written alongside a running environment. See
`docs/DATA_EXPLORATION.md` for analysis and `docs/AGENT_CONFIGURATION.md` for
model configuration. Compose records the full local OTel projection in the
same named volume (`otel-spans.jsonl`) and the viewer joins it to a trace by
the persisted OTel trace ID. SQLite remains the canonical replay record.
