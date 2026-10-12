# Platform deployment changelog

## Hosted Firestore catalogue — 2026-10-11

- Publish build-generated environment declarations and checked-in template paths
  to the shared `groundwork_catalogues` Firestore collection before API rollout.
  Pin each revision to an immutable content hash, read it once per process, and
  reuse the SQL-free catalogue adapter for hosted catalogue routes.
- Preserve local file-backed Compose staging, per-owner experiment counts and
  private compiler/evidence snapshots. Missing or corrupt hosted snapshots fail
  explicitly; requests cannot silently rebuild a temporary catalogue database.
- Add a create/get-only build publisher role and document the one-time bootstrap
  prerequisite. Publication verifies read-back and rejects snapshots above 900 KB.
  Old snapshots remain available for rollback. This PR stages code and tests; it
  does not change live GCP permissions or claim a hosted deployment.


## Catalog publication with scoped storage access — 2026-10-11

- Publish runtime bindings through an exact-object GCS JSON API upload with
  `ifGenerationMatch=0`. Avoid the bucket-wide object listing performed by
  `gcloud storage cp`, preserving the build identity's catalog-prefix grants.
- Accept an existing catalog only when its bytes match the expected content
  hash. Upload or verification failures stop publication before API rollout.


## First runtime publication lookup — 2026-10-11

- Recognize the exact `gcloud artifacts docker images describe` diagnostic
  `Image not found.` when the first immutable runtime tag has not been published.
  Build the missing runtime while continuing to reject permission, network and
  other lookup failures before any publication or API rollout.


## Independent runtime and worker pins — 2026-10-11

- Keep each experiment's runtime image, source SHA and frozen plan immutable while
  resolving the current worker infrastructure for every new launch. Locked older
  experiments receive worker fixes and live logs without a scientific-design fork.
- Run the current worker and pinned episode runtime in two containers within one
  Cloud Run Job. Shared execution files carry plans, output and completion state;
  the worker handles logging, artifact publication and cancellation.
- Record runtime and worker source SHAs and image digests, plus the worker contract
  version, separately on launches and exports. Preserve legacy `source_sha` and
  `image_digest` as runtime aliases. Historical launches retain their original
  provenance; missing worker pins are not inferred or retroactively filled.
- During deployment, publish immutable private runtime/worker bindings and create
  `groundwork-run-<runtimeDigest12>-<workerDigest12>` Jobs for current and supported
  legacy runtimes. Reject images predating the supported platform execution contract.
  No separate migration command is required.
- Show the two image pins in launch inspection and describe missing live logs as
  a historical launch limitation. Both containers remain trusted code with the
  same Job identity, network and execution volume; this is not an IAM isolation
  boundary for untrusted images.
- Read locked designs and cell counts from their frozen snapshots. Reopening or
  launching them does not validate against the current environment catalog or run
  newer scientific metadata backfills; draft validation remains unchanged.
- Reuse the existing runtime image for infrastructure-only commits by resolving
  the latest scientific-input commit in CI. Worker updates do not advance the
  default experiment runtime. Paired Jobs share a 512 MiB memory budget and accept
  only registered v1 execution-contract runtimes.

This entry describes implementation on the separation branch. Hosted publication
and a successful execution through the new binding are verified separately.

## Local developer catalogue

- Generate release metadata automatically from installed game packages during
  image builds. Local APIs persist a content-addressed JSON catalogue on the
  existing data volume and reuse it instead of constructing catalogue databases
  for each request. Private user experiment counts remain separate.
- Preserve catalogue API contracts, item browsing, agent choices and validation;
  compiler and runner SQLite snapshots remain private execution artifacts.
- Allow an alternate local image tag so independent Compose staging stacks do
  not overwrite the shared default image. No Firebase emulator is required.

## Live launch logs and progress — 2026-10-11

- Publish unbuffered runner output and provisional episode progress during
  execution, through a private snapshot refreshed about every two seconds.
  Snapshots retain the newest 200 log entries within 64 KiB; the full runner
  output and final scientific evidence remain in immutable launch artifacts.
- Stream authenticated launch logs into the existing launch inspection screen,
  with stable log IDs, reconnect/resume, connection status, and terminal closure
  after the final output and progress arrive. Short stream sessions and durable
  backing storage allow reconnection to any control-service instance.
- Keep ownership checks on both initial reads and streams. Session expiry stops
  a live stream; provisional progress cannot make a launch evidence-complete.
- Historical launches retain the worker release they used. Their persisted logs,
  results and execution provenance remain available; new executions use the
  independent worker binding described above.
- The production deployment workflow now triggers on `main`; the temporary
  verification-branch push trigger has been removed for the merge checkpoint.
- Dropped Python 3.11 support: CI runs one Python 3.12 test job, and shipped
  package metadata, the game scaffold and runtime documentation require 3.12+.

## Initial platform implementation

- Added a separate local Docker Compose control API and durable worker, with
  shared SQLite document metadata and immutable artifact volume.
- Reused the existing scientific design compiler and episode runner; colocated
  environment and agent children communicate over loopback inside the worker.
- Preserved the existing Next.js researcher interface, including the environment
  catalog, design editor, draft/save/lock/fork workflow, experiment evidence,
  episode viewer, and same-origin replay assets. Connected its API to owner-scoped
  metadata and private immutable artifacts rather than mounting the local server.
- Added hosted Google login, expiring application sessions, CSRF validation, and
  owner checks for experiment metadata, launches, episodes and exports.
- Added native Firestore metadata and private GCS launch plans, evidence and
  exports; model-provider credentials are supplied only to worker Jobs.
- Added separate deployment/runtime identities and GitHub OIDC federation with
  exact repository, workflow and branch trust; no long-lived service-account key.
- Added automatic checked-in environment discovery, built-image tests, Artifact
  Registry publication, and image-digest-pinned Cloud Run release Jobs.
- Added hosted health and unauthenticated-denial verification after CI publication.
- Generated Next static environment routes from installed release declarations
  during image builds, so new checked-in games need no frontend ID-list edit.

The first release runs one launch per Job execution, with one task and bounded
episode parallelism. Job runtime is limited to one hour and automatic retries
are disabled. Scientific retries create explicit attempts. Agents within a
launch share resources and are trusted repository code. There are no external
agent callbacks, user-provided containers, or cross-user sharing yet.
Authored `local_process` rosters currently support Calendar baseline scripted
agents; other registered games use `in_process` rosters under the existing
compiler validation.

Exports currently run synchronously within an authenticated request and have a
size/count cap to stay within Cloud Run memory and request duration. The platform
fails oversized requests explicitly; asynchronous export Jobs and sharded
launch scheduling remain follow-up work. Deployment and operational verification
are recorded separately from implementation; this changelog does not assert that
Google login or a hosted experiment has passed until those checks are completed.

## Deployment verification checkpoint — 2026-10-11

- Google sign-in succeeded on the hosted control service with the real web OAuth
  client; hosted development login is disabled and unauthenticated private routes
  return 401.
- The first hosted scripted study completed two episodes through Cloud Tasks and
  the digest-pinned Cloud Run Job. Its Firestore metadata and private GCS export
  joined correctly; all 44 exported artifact checksums passed. The authenticated
  browser download endpoint returned the complete ZIP with HTTP 200.
- The restored interface passed its original browser workflow regressions. A
  rebuilt local Compose stack completed two episodes, verified 44 export checksums,
  and passed 17 anonymous/cross-user/CSRF checks.
- Fixed anonymous cross-tab CSRF challenge reuse. Added explicit hosted draft
  lock requirements and reusable same-origin ZIP links, with real HTTP download
  regression coverage.

This checkpoint's hosted execution used source `4ee748d74cee9bdf6312a286d36495b3457654d1`.
The final interface release and CI publication are verified separately in the PR;
these checks do not claim that a paid model-provider call or a second Google
account was exercised. The hosted study used scripted agents. Drafts need to be
locked before hosted smoke, dry-run, or live execution; the standalone local
control plane retains its original draft smoke behavior.

## CI publication API prerequisite

- Enabled Cloud Resource Manager during bootstrap. CI resolves the project
  number to publish the stable Cloud Run URL registered for Google login; this
  lookup requires the API to be enabled. The existing deployment identity's
  Cloud Run Developer role already provides the project read permission, so
  this fix requires no broader IAM grant.
- Selected the Cloud SDK image's maintained CA bundle for bundled Python. This
  preserves HTTPS certificate verification while allowing the CI health and
  authentication boundary checker to validate the published service.
