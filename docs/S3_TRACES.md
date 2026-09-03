# Optional S3 trace mirror

S3 is an optional trace mirror, not a requirement for running a environment or
collaborating locally. Start with the SQLite stack in `docs/LOCAL_STACK.md`.

## Configuration

Keep all account details outside the repository:

```yaml
storage:
  backend: s3
  bucket: ${A2A_TRACE_BUCKET}
  prefix: ${A2A_TRACE_PREFIX:-episodes}
  profile: ${AWS_PROFILE:-}
```

Set those release variables or use your deployment's workload identity.
Never commit an AWS account ID, bucket name, access key, or profile name to an
experiment YAML or documentation.

## Behaviour

The S3 store writes a local JSON trace first and mirrors it remotely on a
best-effort basis. An S3 failure is captured in the manifest and never discards
a completed environment. Use `a2a-run EXPERIMENT --smoke-test` to validate the local
data path without credentials; use the provider's standard tooling or workload
identity to validate a remote deployment.

The runner deliberately does not ship lab-specific upload, download, indexing,
or access-management scripts. Those are deployment concerns; build them in the
infrastructure repository that owns the bucket.

## Sharing a study

Two portable workflows are supported, and neither needs an account-specific
script committed here:

1. Run and inspect a study locally with `docker compose up --build`. Traces
   persist to SQLite and appear in the local viewer and control plane.
2. Configure this optional S3 sink through release variables, keeping the
   bucket and identity outside the repository.

Whoever operates a shared service should keep its deployment automation with
that service and preserve the `EpisodeManifest` alongside every trace. For a
portable score view, the local control plane rebuilds Calendar's OpenSkill
snapshot directly from SQLite; other games join a leaderboard by supplying a
environment-owned `RatingEvent` adapter, described in `docs/DATA_EXPLORATION.md`.

See `docs/STORAGE.md` for all sink semantics and configuration precedence.
