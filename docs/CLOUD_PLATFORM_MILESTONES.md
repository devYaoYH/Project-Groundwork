# Local-first to managed-platform milestones

This roadmap extends the local-first framework without changing its core
contracts.  It deliberately separates the **control plane** (researcher-facing
authoring, releases, permissions, and run orchestration) from the **episode
data plane** (one or more workers executing an immutable experiment).

The canonical scientific record remains the framework-owned resolved
configuration, `GameTraceBase`, typed environment/episode references, and
`RunManifest`.  Firestore, Redis, OpenTelemetry/Langfuse, BigQuery, and Cloud
Run are integration and projection layers around that record; none owns its
schema or replaces it.

## Verified starting point

The current repository is a working local-first baseline:

- `docker compose up --build --abort-on-container-exit runner` builds and
  completes the four-game SQLite smoke run; the Compose viewer is healthy.
- `a2a-run experiments/all_games_smoke.yaml --smoke-test` persists and reads
  back Calendar and Negotiation (as well as the two reference games) without
  credentials or cloud access.
- Calendar's typed-environment smoke experiment and Negotiation's standalone
  SQLite-default smoke experiment both pass.
- The offline regression suite and `python scripts/release_check.py` pass.

The release gate is nevertheless a product decision, not just a test result:
resolve the negotiation-data licence/visibility decision and approve the trace
retention and redaction policy before declaring the public local MVP complete.

## Milestone 0 — Release-ready local MVP

Finish and retain the existing cloud-independent path.

- SQLite/local storage is the supported default; S3 and Firestore remain
  explicit optional sinks.
- Compose runs the runner, viewer, SQLite trace corpus, and local OTel
  projection.
- Calendar scoring inputs and derived artifacts remain digest-bound to completed
  traces.
- Release documentation, licensing, retention/redaction rules, and public
  release scans are accurate and enforced.

**Exit condition:** a new contributor can clone, run the smoke suite and
Compose stack, inspect traces/spans, and replay/rebuild local analysis with no
cloud account or model key.

## Milestone 1 — Local Web UI and control-plane contract

Build the Web UI/control plane now, but make its first execution backend local.
This brings the researcher workflow forward without forcing a cloud dependency.

- Add a UI plus a small API service to the Compose stack.
- Start Redis locally with append-only persistence. Every episode appends its
  normalized game events to a deterministic rollout/episode stream; the API
  proxies those streams to browser replay applications and preserves them as a
  crash-recovery input until the canonical trace is finalized.
- Define versioned, framework-owned control-plane records: `GameRelease`,
  `Experiment`, `Rollout`, and `EpisodeAttempt`.  These reference immutable
  environment/config digests and trace IDs; they do not duplicate trace event
  schemas.
- Add a `RunLauncher` boundary with a `LocalLauncher` that invokes the installed
  game release in a subprocess or dedicated local runner container.
- Persist control-plane metadata in a local SQLite database.  Keep the existing
  trace SQLite database as the canonical local trace store.
- Support authoring/selecting a declared experiment, validating it, launching a
  rollout, tracking episode state, cancellation, retry, trace links, and a
  local SSE endpoint for filtered progress updates. The local runner supports
  both explicit live execution and a credential-free smoke mode.

The control plane chooses a registered game release and a validated experiment
configuration.  It does **not** accept browser-submitted Python, Dockerfiles,
or runtime image builds.

**Exit condition:** a researcher can use the local UI to select Calendar or
Negotiation, launch a local multi-episode rollout, watch its state, and open
the resulting trace/OTel view using the same CLI-compatible artifacts.

Before implementation, approve a short RFC for the control-plane API and the
four records above.  This is a new formal contract and deserves the same review
discipline as the existing typed trace and environment contracts.

## Milestone 2 — Contribution packaging and release CI/CD

Make a game environment directory the sole build input.  Contributions merge
through source control; the UI only selects already-approved releases.

- Standardize the release metadata each `games/<game>/` directory supplies:
  package entry point, supported environment/config schema versions, test
  commands, runner image recipe, and declared runtime capabilities.
- On pull requests, run schema validation, offline tests, adapter/trace
  contract tests, and a deterministic smoke execution for changed games.
- On a protected merge or signed tag, Cloud Build builds the declared game
  runner image, scans it, emits an SBOM/provenance attestation, and publishes it
  to private Artifact Registry.
- Register an immutable `GameRelease` using the source commit, package versions,
  environment/config schema digests, and **image digest** (never a mutable
  tag).  The release is then available to the local or managed launcher.
- Retain a local image build path so contributors can validate the same release
  recipe before Cloud Build is enabled.

**Exit condition:** a merged Calendar or Negotiation release automatically has
a tested, immutable runner image that the control plane can select by digest.

## Milestone 3 — Managed durable execution substrate

Add GCP implementations behind the already-tested control-plane and storage
boundaries.

- Implement `CloudRunJobLauncher`; one rollout creates one Cloud Run Job
  execution with `taskCount = episode_count` and explicit bounded parallelism.
  Each task derives its episode ID and seed from the immutable rollout input and
  Cloud Run task index.
- Use a Cloud Run **Job**, not N long-lived HTTP requests, for independent
  long-horizon episodes.  Configure task timeout, retries, parallelism, CPU,
  memory, and maximum execution budget per release.
- Introduce a shared durable trace sink appropriate for GCP (normally a
  GCS-backed `TraceStore` with local staging and immutable trace/manifest
  objects).  Firestore may hold small control-plane metadata, but is not the
  required canonical event archive.
- Add a cloud implementation of the control-plane metadata store, preserving
  the Milestone 1 record schema and migrations.
- Pass the rollout input by immutable object URI/digest, not mutable environment
  variables or per-task handwritten payloads.

**Exit condition:** the same UI request can be launched locally or as a bounded
Cloud Run Job, and both paths yield equivalent resolved configuration, manifest,
and replayable trace records.

## Milestone 4 — Live telemetry and analytical projections

Add observability and live viewing without making either a source of truth.

- Publish a small, redacted, versioned progress-event projection to Redis
  Streams (or an equivalent managed event channel) for active rollouts only.
  The control plane authenticates the browser and exposes SSE; browsers do not
  read tenant streams directly.
- Preserve the full event trace through the durable `TraceStore` path.  Redis
  keys are tenant- and rollout-scoped, TTL-bound, and deleted only after durable
  completion has been verified.
- Export OTel spans asynchronously to a configured OTLP or Langfuse backend,
  retaining the framework trace ID for joins.  Content capture follows the
  established redaction policy.
- Materialize completed, schema-versioned, idempotent BigQuery tables from
  durable traces and derived artifacts.  BigQuery is an analytics projection,
  not a live coordination database or the replay record.

**Exit condition:** an authorized researcher sees sub-second redacted progress,
can open the final canonical trace and correlated spans, and can query a
rebuildable BigQuery projection for completed rollouts.

## Milestone 5 — Multi-tenant hardening and operating limits

Make the hosted path safe and predictable before scaling its audience or quota.

- Authenticate users at the control plane and authorize every experiment,
  release, rollout, trace, artifact, stream, and query by tenant scope.
- Use dedicated least-privilege service identities for the control plane, build
  pipeline, and workers.  Do not begin with per-researcher OBO service-account
  impersonation; add it only when an approved data-access requirement needs it.
- Restrict worker egress and secrets, pin image digests, verify build
  provenance, set per-tenant concurrency/model-spend limits, and enforce
  budget alerts.
- Make launch/retry/archive operations idempotent; record episode attempts and
  provide failure classification, retry limits, cancellation, and audit logs.
- Test retention, redaction, deletion, disaster recovery, and cross-tenant
  isolation under load before increasing Cloud Run parallelism.

**Exit condition:** the platform can safely run bounded concurrent rollouts for
multiple researchers with auditable authorization, cost controls, and recovery
semantics.

## Relationship to the original kernel roadmap

The typed interoperability, OTel, adapters, local test ladder, and declarative
environment work remain cross-cutting foundations.  Much of that work is now
present in the repository, so it should be completed incrementally alongside
Milestones 0–2 rather than held behind a future hosted platform milestone.

The essential ordering is:

```text
local contracts and replay
  → local control plane
  → reviewed game-release CI/CD
  → Cloud Run Job launcher + durable shared trace storage
  → live projections and tenant hardening
```

This preserves local reproducibility, lets the UI become useful immediately,
and ensures managed infrastructure adds scale rather than a second experiment
model.
