# Platform deployment changelog

## Initial platform implementation

- Added a separate local Docker Compose control API and durable worker, with
  shared SQLite document metadata and immutable artifact volume.
- Reused the existing scientific design compiler and episode runner; colocated
  environment and agent children communicate over loopback inside the worker.
- Added hosted Google login, expiring application sessions, CSRF validation, and
  owner checks for experiment metadata, launches, episodes and exports.
- Added native Firestore metadata and private GCS launch plans, evidence and
  exports; model-provider credentials are supplied only to worker Jobs.
- Added separate deployment/runtime identities and GitHub OIDC federation with
  exact repository, workflow and branch trust; no long-lived service-account key.
- Added automatic checked-in environment discovery, built-image tests, Artifact
  Registry publication, and image-digest-pinned Cloud Run release Jobs.
- Added hosted health and unauthenticated-denial verification after CI publication.

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
