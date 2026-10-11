# Platform deployment changelog

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
