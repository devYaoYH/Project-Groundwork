# Groundwork platform deployment

The local and hosted platforms execute the same pinned experiment plans. One
launch owns a worker sandbox; its runner runs episodes with bounded parallelism
and starts agent processes on loopback. No service-instance addressing is needed.
The control pane cannot access model-provider credentials.

## Local first

```sh
docker compose -f compose.platform.yml up --build --detach --wait
```

Open <http://localhost:8080>. Local development login supports Alice and Bob so
ownership and forbidden cross-user reads can be verified without cloud accounts.
The worker reads provider keys from environment interpolation; scripted smoke
experiments need no keys. Metadata and immutable artifacts survive restarts in
the `platform-data` named volume. This is a separate stack from the existing
credential-free runtime examples in `docker-compose.yml`.

For real local Google login, create a **Web application** Google OAuth client,
add `http://localhost:8080` as an authorized JavaScript origin, set
`GOOGLE_OAUTH_CLIENT_ID`, and disable `PLATFORM_DEV_AUTH` in the API environment.
The Google credential is verified server-side, then replaced by an expiring
HttpOnly application session. Production refuses development login.

## Hosted resources

The default target is `cicl-487801`, region `us-central1`. Bootstrap creates only
Groundwork-named resources:

- `groundwork-control` Cloud Run control service, publicly reachable login and
  assets, with application authentication on private API routes.
- `groundwork-ep-<commit>-<digest>` digest-pinned Cloud Run Jobs, one execution per launch,
  one task per execution, with agent children in that sandbox. Automatic retries
  are zero; scientific retries are explicit new attempts. Jobs have a one-hour
  execution limit and bounded episode parallelism of two.
- Native Firestore `groundwork_*` metadata collections in the existing default
  database, with application-enforced owner filtering and detail checks. Server
  libraries bypass Firestore rules; these backend checks are the security boundary.
- Private, public-access-blocked GCS artifacts and Cloud Build source buckets.
  Exports are served through the authenticated API, with experiment metadata,
  traces, configs, release provenance, metrics, and checksums.
- Cloud Tasks queue `groundwork-dispatch` invokes the authenticated dispatch
  endpoint. API dispatch starts a Job execution and returns immediately.
- Separate API, worker, dispatch, build and CI identities. Runtime identities
  can access experiment data; CI has short-lived build submission credentials.
  Build can deploy services and Jobs but cannot change project IAM.

Run as a project administrator:

```sh
python deploy/bootstrap.py --project cicl-487801 --region us-central1
```

No provider secret is created with an empty value. To enable a provider, add a
Secret Manager secret with its real value, then grant `groundwork-worker` the
`roles/secretmanager.secretAccessor` role **on that secret**. Supported names are
`groundwork-openai-api-key`, `groundwork-anthropic-api-key`,
`groundwork-google-api-key`, and `groundwork-openrouter-api-key`. Only the worker
receives these references during deployment. `groundwork-session` is generated
by bootstrap and available only to the control API.

Create a Google Web OAuth client and add the deployed Cloud Run URL as an
authorized JavaScript origin. A client ID is required for deployment; its client
secret is unnecessary for Google Identity Services token login. Build the first
image with the checked-in build-only config before initializing service IAM:

```sh
gcloud builds submit . --project cicl-487801 --region us-central1 \
  --config deploy/cloudbuild-image.yaml \
  --gcs-source-staging-dir gs://cicl-487801-groundwork-build-source/source \
  --substitutions _SOURCE_SHA=FULL_COMMIT
```

Initial deployment
requires an administrator to run `deploy/release.py` once with
`--initialize-public`; subsequent CI preserves that IAM policy.

```sh
python deploy/release.py --project cicl-487801 --region us-central1 \
  --image us-central1-docker.pkg.dev/cicl-487801/groundwork/platform:COMMIT \
  --source-sha FULL_COMMIT --oauth-client-id CLIENT.apps.googleusercontent.com \
  --initialize-public
```

## GitHub → Cloud Build → shared release

```sh
python deploy/configure_github.py \
  --oauth-client-id CLIENT.apps.googleusercontent.com
```

This writes nonsecret repository variables and provisions Workload Identity
Federation restricted to the numeric repository and owner IDs, the platform
workflow, and `main` push/manual events. No service-account key is stored in
GitHub. Pull requests run platform tests without cloud credentials. Trusted main
pushes validate, submit Cloud Build, test the built image, push Artifact Registry,
and deploy a digest-pinned release for all experimenters. All checked-in game
packages declaring `a2a_engine.environments` are automatically installed.

For first verification before merge, pass `--verification-branch codex/platform-deployment`
to trust exactly that workflow branch, then push the reviewed implementation.
The workflow has a temporary explicit push trigger for this branch because a
new manual workflow cannot be dispatched before it exists on the default branch.
After merge, remove the temporary push trigger and rerun the configuration command
without that flag to remove the temporary trust. Protect `main` with required
reviews and CI checks appropriate to the repository's contributor model.

Cloud Build uses the `groundwork-build` identity and Cloud Logging. To submit the
same pipeline manually:

```sh
gcloud builds submit . --project cicl-487801 --region us-central1 \
  --config cloudbuild.yaml \
  --gcs-source-staging-dir gs://cicl-487801-groundwork-build-source/source \
  --substitutions _SOURCE_SHA=FULL_COMMIT,_GOOGLE_OAUTH_CLIENT_ID=CLIENT.apps.googleusercontent.com
```

Verify that unauthenticated private requests fail, two experimenters cannot read
each other's metadata or exports, a scripted hosted launch completes and persists
its artifacts, and a GitHub workflow records a successful Cloud Build deployment.
Image digests and source SHA are recorded in the deployment output and release
metadata. Retain old Jobs while experiments reference them; do not rewrite old
release images.

```sh
python deploy/verify_hosted.py --project cicl-487801 --source-sha FULL_COMMIT
```

CI runs this health, Google login configuration, and unauthenticated-denial check
after publication. Real Google login and completed experiment/export checks still
require an authenticated end-to-end run.

## Current limits

Local colocation shares CPU/memory and cannot isolate untrusted agent code. Hosted
execution is isolated per launch, not per seat. Large launches remain bound to one
Job task and its one-hour duration; split them into explicit launches before
increasing scale. External human/agent callbacks and direct per-seat hosting are
future work. A future scheduler can shard immutable plans across Job tasks while
preserving attempt IDs. Owner-based access is implemented by the backend; there is
no cross-user leaderboard or collaboration sharing in this initial release.
