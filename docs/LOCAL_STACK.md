# Local collaboration stack

The Compose stack is the supported cloud-free onboarding path. It runs all
packaged environments, writes episodes to a named-volume SQLite database, and
hosts the researcher control plane. It uses no AWS, GCP,
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

The legacy in-process stack can receive model credentials through Compose interpolation; do not use that stack for the isolated agent deployment described below. A local OpenAI-compatible model
server can be reached from Docker Desktop through
`http://host.docker.internal:<port>/v1`; configure that endpoint in a private
experiment YAML or release-specific overlay.

## Isolated agent runtime: CLI and viewer

`docker-compose.runtime-launch.yml` is a standalone supported deployment, not an overlay on the legacy credential-bearing stack. It runs the same runner and baked-in viewer with five long-lived agent services, a private internal runtime network and separate read-only per-seat descriptor mounts. The viewer also joins a separate UI bridge so Docker Desktop can publish its localhost port (`8082`); agents never join that bridge and have no workspace, trace database, sibling tickets or Docker socket. The UI bridge is not an outbound firewall for the viewer, but the viewer receives no provider credentials. Provider credentials are absent by default. Do not combine it with `docker-compose.yml`.

```bash
docker compose --env-file /dev/null -f docker-compose.runtime-launch.yml up --build --detach --wait
docker compose --env-file /dev/null -f docker-compose.runtime-launch.yml exec -T viewer \
  a2a-run experiments/all_games_runtime_smoke.yaml --smoke-test \
  --smoke-episodes-per-cell 2 --max-parallelism 4 --storage-path /data/a2a.db --results-dir /data/results
```

CLI execution uses `exec viewer` because the environment advertises the exact `viewer` service origin; an unrelated ephemeral runner container must not advertise itself as `viewer`. The ordinary `scripts/run_game_runtime.py --release ... --experiment ...` wrapper also uses this runner/provenance path, not a second orchestration implementation. Compose execution requires an explicit non-human roster of at most five seats, uses `/2`, and converts local runtime choices to isolated external seats without mutating the original config. Scripted smoke never constructs providers.

Open `http://127.0.0.1:8082/`. Each game's checked-in `games/<folder>/experiments/runtime_smoke.yaml` supports browser import and smoke launch. For example:

```bash
curl -X POST http://127.0.0.1:8082/api/experiments -H 'Content-Type: application/json' \
  -d '{"yaml_path":"games/calendar/experiments/runtime_smoke.yaml"}'
curl -X POST http://127.0.0.1:8082/api/launches -H 'Content-Type: application/json' \
  -d '{"experiment_id":"<returned-id>","mode":"smoke","max_parallelism":2}'
```

Health checks precede private one-time provisioning. Each game gets its own environment tool server on one of 32 exact ports (`8100`–`8131`); concurrent jobs retain independent episode/attempt/seat identities and agent histories. Capacity exhaustion fails explicitly. Successful, failed and SIGINT/SIGTERM-cancelled jobs revoke pending authority and remove descriptors; the agent watcher cancels only the corresponding session, not its service or sibling jobs. Runtime crashes expire turns rather than resume them; an abrupt environment kill can leave descriptor files behind even after their tickets expire, so graceful cancellation is preferred. Named data and descriptor volumes survive service recreation.

### Optional live Calendar with agent-only credentials

`games/calendar/experiments/runtime_deepseek_v4_flash.yaml` is one bounded live episode: three `deepseek/deepseek-v4-flash` seats through OpenRouter's OpenAI-compatible API, six slots, two meetings, two talk turns, one decision retry, environment reflections and a 120-second turn deadline. It contains no key. The operator must explicitly approve and execute the live run; a passing smoke test is not live-provider validation.

Set `OPENROUTER_API_KEY` in the invoking shell using your secret-management workflow. Do not add it to experiment YAML or print a resolved Compose configuration. The optional provider overlay injects the key and provider-egress network only into agent services; the viewer remains credential-free on its runtime and UI networks. These commands enable the deployment and then make real, billable provider calls (omit neither the isolation file nor the provider overlay):

```bash
docker compose --env-file /dev/null -f docker-compose.runtime-launch.yml -f docker-compose.runtime-provider.yml up --build --detach --wait
docker compose --env-file /dev/null -f docker-compose.runtime-launch.yml -f docker-compose.runtime-provider.yml exec -T viewer \
  python scripts/run_game_runtime.py --release games/calendar/runtime/release.json \
  --experiment games/calendar/experiments/runtime_deepseek_v4_flash.yaml \
  --storage-path /data/a2a.db --max-parallelism 1
```

For browser execution, import that same YAML and launch with `mode: live`; readiness checks only provider-presence booleans inside the isolated agent services. The current adapter is structured-output, not native model tools. The example's model ID and account availability are not verified until the live provider run. `--smoke-test` is a safe credential-free rehearsal, not the final live command.

Before visual review, rebuild/recreate this deployment with its `up --build --detach --wait` command and verify `ps` health. The Next.js export is baked into the image; a workspace mount alone cannot refresh it, and the browser may need a hard refresh. For the legacy default stack, use `docker compose up --build --detach --wait viewer`. Use `down` without `--volumes` to stop a persistent stack; never delete data/Redis volumes just to refresh code.

## Launch experiments from the browser

`http://localhost:8080/` is the researcher control plane: a Next.js static
export, built by a Node stage in the same image and served by this Python
server from `--static-dir`. There is no second service and no Node at runtime.

| Screen | Route | What it is for |
|---|---|---|
| Environments | `/environments/` | what each installed release declares |
| Environment detail | `/environments/<id>/` | parameter table, measures, oracle playground, executable workflow |
| Item bank | `/environments/<id>/items/` | the frozen corpus and its oracle results |
| Experiments | `/experiments/` | the researcher's landing page |
| Design | `/design/?id=<experiment>` | write, validate live, compile, lock |
| Experiment | `/experiment/?id=<experiment>` | progress by cell, launches, episodes |
| Episodes | `/episodes/` | the filtered, paginated fact table |
| Episode | `/episode/?uid=<episode_uid>` | lanes, scrubber, transcript, config |

An environment's own replay page is served beside them at
`/environment-replays/<environment>/`, and the episode screen mounts one for any
environment that registers it — driven by the same cursor as the lane view. See
`docs/VIEWER_EXTENSIONS.md`.

The browser never uploads Python, a Dockerfile, or an image: it only selects a
release that is already installed and authors a design against its declaration.

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
| control plane and web UI | standard-library `local_stack/server.py` serving the Next.js static export, including local OTel span correlation | a hosted API serving the same contracts |
| leaderboard | Calendar snapshot replayed from completed SQLite episodes | shared artifact/materialization store |

The image is suitable for a Kubernetes Job: it needs an experiment YAML,
release variables, and writable storage. SQLite is deliberately
single-host/single-volume; do not mount one SQLite file read-write from multiple
nodes. For a cluster, retain the same runner image and replace only the storage
configuration with a shared backend when that integration is ready.

## API and data contract

| Endpoint | Answers |
|---|---|
| `GET /api/health` | readiness plus the episode count |
| `GET /api/environments` | the installed release catalog |
| `GET /api/environments/<id>` | declaration, plus item levels projected from the pinned bank |
| `GET /api/environments/<id>/workflow` | read-only executable LangGraph structure, without launching an episode |
| `GET /api/environments/<id>/items` | one page of the item bank |
| `POST /api/environments/<id>/oracle` | run the release's oracle on one item (409 when it ships none) |
| `POST /api/designs/validate` | validate and compile a draft without storing it |
| `GET`/`POST /api/experiments` | the experiment library; create from a design or a checked-in YAML |
| `GET /api/experiments/<id>` | cells, roster, launches, progress by cell |
| `POST /api/experiments/<id>/design` | save an editable design draft |
| `POST /api/experiments/<id>/fork` | fork a locked preregistration into an editable draft |
| `POST /api/experiments/<id>/lock` | preregister (409 on digest mismatch) |
| `GET`/`POST /api/launches` | launch list; start one (409 for `live` on an unlocked design) |
| `GET /api/launches/<id>` | one launch and its progress |
| `GET /api/launches/<id>/events` | SSE launch event log |
| `POST /api/launches/<id>/cancel` | cancel a launch |
| `GET /api/episodes` | the filtered, paginated fact table |
| `GET /api/episodes/<episode_uid>` | one episode, plus its lanes, `index_label` and `cursor_max` |
| `GET /api/episodes/<episode_uid>/artifacts` | digest-bound derived artifacts |
| `GET /api/episodes/<episode_uid>/observability` | correlated local OTel spans |
| `GET /api/streams/<stream>` | a Redis event stream, and `/trace` for its projection |
| `GET /api/leaderboards/calendar` | the Calendar OpenSkill snapshot |

The trace corpus itself stays read-only: the runner writes source episodes and
post-hoc analysis can add only digest-bound derived artifacts.

`GET /api/episodes/<episode_uid>` answers
`{episode, lanes, index_label, cursor_max}`. The trace rides under `episode`
exactly as the store holds it; `lanes` (the pinned roster, read from the
episode's own provenance) and `index_label` (from the release declaration) are
read-time projections and ride *alongside* the record rather than inside it, so
what is durable and what is derived are never confusable in the payload.

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
