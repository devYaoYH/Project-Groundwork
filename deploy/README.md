# Groundwork platform deployment

The local and hosted platforms execute the same pinned experiment plans. One
launch owns a worker sandbox; its runner runs episodes with bounded parallelism
and starts agent processes on loopback. No service-instance addressing is needed.
The control pane cannot access model-provider credentials. Hosting preserves the
existing experiment UI, design editor, environment/item views, launch progress,
episode lanes, and replay screens; Google login wraps that interface.

Launch inspection streams runner output and provisional episode progress while
the worker executes. Publication and stream polling each occur about every two
seconds, so output typically appears within a few seconds rather than waiting
for the launch to finish. The live view retains the newest 200 log entries within
64 KiB; the complete `runner.log` remains in the final export. Streams resume
using stable log IDs after disconnections and stop when the launch ends or the
application session expires. Episode trace links become available when final
evidence has been published.

Experiment execution and worker infrastructure have independent image pins.
The experiment retains its runtime source SHA, image digest and frozen plan;
each new launch resolves the currently deployed worker. Worker fixes, live logs,
artifact publication and cancellation therefore apply to a new execution of an
older locked experiment without changing its game code or requiring a fork.
Forking remains the way to change a locked scientific design. Existing launch
records and results retain the worker they actually used and are never rewritten.

Hosted Jobs run two containers: the current worker coordinates the execution,
while the pinned runtime runs the episode runner and agent children. They exchange
plans, output and completion state through shared execution files. Both runtime
and worker source SHAs and image digests, plus the worker contract version, are
recorded on each launch and included in exports. Legacy `source_sha` and
`image_digest` fields remain aliases for the experiment runtime; unknown worker
provenance in historical records is left absent.

Deployment automatically creates `groundwork-run-<runtimeDigest12>-<workerDigest12>`
Jobs for the newly published runtime and supported legacy `groundwork-ep-*`
runtime images. The current API loads their bindings from an immutable private GCS
catalog. Existing experiments keep their original runtime pin while their next
launch uses the current worker binding. There is no separate migration command.
Runtime images predating the supported platform execution contract are rejected
explicitly; deployment does not guess how to execute an incompatible image.

## Local first

```sh
docker compose -f compose.platform.yml up --build --detach --wait
```

Open <http://localhost:8080>. Local development login supports Alice and Bob so
ownership and forbidden cross-user reads can be verified without cloud accounts.
The runtime container reads provider keys from environment interpolation; scripted smoke
experiments need no keys. Metadata and immutable artifacts survive restarts in
the `platform-data` named volume. This is a separate stack from the existing
credential-free runtime examples in `docker-compose.yml`.

The local platform uses a file-backed release catalogue on the existing named
volume. The image build discovers every installed game entry point and exports
its declarations and checked-in experiment templates into a JSON manifest.
The API publishes a content-addressed catalogue snapshot once, then reuses it
for release listing, environment metadata, item browsing, agent choices and
validation. Private experiment counts are queried separately for the signed-in
user; they are never stored in the shared catalogue.

To stage a new game, add its package under `games/` with an
`a2a_engine.environments` entry point and a release declaration, then rerun the
Compose build command above. Package installation, catalogue discovery and
frontend environment routes are automatic. Game developers do not need to edit
Compose, Dockerfiles, service settings or a hard-coded environment list. Direct
Python development discovers installed games on first use; restart the API after
installing or changing a game. Local staging needs no Firebase emulator or cloud
credentials. The scientific compiler and runner still use their private SQLite
snapshots. Hosted catalogue publication uses Firestore, as described below.

For an isolated staging stack, use a separate Compose project, image tag and port:

```sh
PLATFORM_IMAGE=groundwork-platform:catalogue PLATFORM_PORT=18082 \
  docker compose -p groundwork-catalogue -f compose.platform.yml up --build --detach --wait
```

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
- `groundwork-run-<runtimeDigest12>-<workerDigest12>` Cloud Run Jobs pin both container
  images, with one execution per launch and one task per execution. Agent children
  run in the runtime container within that sandbox. Automatic retries
  are zero; scientific retries are explicit new attempts. Jobs share a 512 MiB
  memory budget, have a one-hour execution limit and bounded episode parallelism
  of two. Only registered v1 execution-contract runtimes are supported.
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

Before the first deployment of independent worker/runtime versions, rerun
bootstrap to create the immutable-tag `groundwork-runtime` Artifact Registry
repository and grant the build identity create/read access restricted to the
`platform/runtime-bindings/` artifact prefix. Subsequent releases discover and
retain the existing digest-pinned legacy runtime images automatically.
Catalog publication uses an exact-object JSON API upload with a generation-zero
precondition; the build identity does not need bucket-wide object listing.

Compose uses mutable development tags and `local` provenance placeholders by
default. Hosted source commits, image digests and binding catalogs enforce the
immutable runtime history described above.

No provider secret is created with an empty value. To enable a provider, add a
Secret Manager secret with its real value, then grant `groundwork-worker` the
`roles/secretmanager.secretAccessor` role **on that secret**. Supported names are
`groundwork-openai-api-key`, `groundwork-anthropic-api-key`,
`groundwork-google-api-key`, and `groundwork-openrouter-api-key`. These references
are scoped to launch Jobs, never the control API. `groundwork-session` is generated
by bootstrap and available only to the control API.

Create a Google Web OAuth client and add the deployed Cloud Run URL as an
authorized JavaScript origin. Use the deterministic URL
`https://groundwork-control-1011791564032.us-central1.run.app` for this project;
deployment output reports it as `service_url`, and Cloud Tasks and CI verification
use the same URL. Cloud Run also reports a hashed infrastructure URL, which needs
its own authorized OAuth origin if used for browser login.
A client ID is required for deployment; its client
secret is unnecessary for Google Identity Services token login. Build the first
image with the checked-in build-only config before initializing service IAM:

```sh
gcloud builds submit . --project cicl-487801 --region us-central1 \
  --config deploy/cloudbuild-image.yaml \
  --gcs-source-staging-dir gs://cicl-487801-groundwork-build-source/source \
  --substitutions _SOURCE_SHA=FULL_COMMIT,_RUNTIME_SOURCE_SHA=SCIENCE_COMMIT
```

Initial deployment
requires an administrator to run `deploy/release.py` once with
`--initialize-public`; subsequent CI preserves that IAM policy.

```sh
python deploy/release.py --project cicl-487801 --region us-central1 \
  --image us-central1-docker.pkg.dev/cicl-487801/groundwork/platform:COMMIT \
  --source-sha FULL_COMMIT \
  --runtime-image us-central1-docker.pkg.dev/cicl-487801/groundwork-runtime/runtime:SCIENCE_COMMIT \
  --runtime-source-sha SCIENCE_COMMIT --oauth-client-id CLIENT.apps.googleusercontent.com \
  --catalogue-sha256 PUBLISHED_CATALOGUE_SHA256 --initialize-public
```

## Hosted shared catalogue

Cloud Build publishes `/opt/groundwork-catalogue.json` from the tested platform
image into `groundwork_catalogues` in the existing default Firestore database.
Each immutable document is addressed by the SHA-256 of its canonical manifest.
`manifest_json` preserves nested declaration arrays without Firestore array
restrictions. Snapshots over 900 KB fail publication before the API rolls out;
a larger catalogue will need sharding. No collection scans or composite indexes
are required.

Publication completes and verifies persisted content before `deploy/release.py`
receives `--catalogue-sha256`. The deployed revision gets
`PLATFORM_CATALOGUE_SHA256`; its first catalogue request reads that document,
then reuses the in-memory adapter across all users. Requests never rediscover
installed games, construct a catalogue database, or write catalogue metadata.
Missing or corrupt pins fail explicitly rather than rebuilding. Older snapshots
remain available for older revisions and rollback. Item banks, workflows and
oracle execution still use the matching packaged game assets; private compiler
snapshots remain unchanged.

Before merging the first release using this workflow, apply the updated bootstrap
(or the following equivalent IAM setup) with an administrator account. This is a
one-time deployment prerequisite, not a game-author step:

```sh
gcloud iam roles create groundworkCataloguePublisher --project cicl-487801 \
  --title='Groundwork catalogue publisher' --stage=GA \
  --permissions=datastore.entities.create,datastore.entities.get
gcloud projects add-iam-policy-binding cicl-487801 \
  --member=serviceAccount:groundwork-build@cicl-487801.iam.gserviceaccount.com \
  --role=projects/cicl-487801/roles/groundworkCataloguePublisher --condition=None
```

The publisher role has no update, delete, or list permission. Firestore server
IAM is database-scoped, so its get permission can read a private document if its
ID is known; this is not collection-level IAM isolation. There is no browser
Firestore access: authenticated APIs enforce experiment ownership and join
private counts onto the shared catalogue for each user. Cloud Build uses its
existing attached service account and no additional credential file.

For a manual release, run `python -m cloud_platform.publish_catalogue --project
cicl-487801 --manifest /path/to/built-image-catalogue.json --output
.platform-build/catalogue-sha256` in an installed platform environment with ADC,
then pass that file's hash to `deploy/release.py`. Publish the exact tested
image's manifest, not a catalogue discovered from a different local checkout.

## GitHub → Cloud Build → shared release

```sh
python deploy/configure_github.py \
  --oauth-client-id CLIENT.apps.googleusercontent.com
```

This writes nonsecret repository variables and provisions Workload Identity
Federation restricted to the numeric repository and owner IDs, the platform
workflow, and `main` push/manual events. No service-account key is stored in
GitHub. Pull requests run platform tests without cloud credentials. Trusted main
pushes submit a Cloud Build candidate request, download the exact output digests
for GitHub validation, then submit a second Cloud Build request for promotion and
deployment. Cloud Build owns image builds and registry cache writes; GitHub needs
only registry Reader access. All checked-in game
packages declaring `a2a_engine.environments` are automatically installed. The
image first installs those packages, then exports route IDs from the same
registered release declarations used by the API catalog. Next builds static
environment and item pages from that generated JSON, so adding a trusted game
does not require editing a frontend environment list. A game that fails to
register fails the image build rather than silently disappearing.

The workflow determines the latest commit affecting scientific runtime inputs and
reuses that runtime tag's existing immutable digest when only infrastructure code
changes. A logging or storage fix publishes a new worker and binding catalog while
retaining the default experiment runtime. Scientific input changes publish a new
runtime; older locked experiments continue to reference their original image.

Cloud Build stages new images under run-specific candidate tags. Its manifest
binds full source SHAs, build UUID, image digests and configuration IDs. GitHub
downloads and verifies those artifacts before Compose and release-image tests.
Validation seals their IDs; the required gate must pass before publication.
The second request copies new science into the immutable runtime repository,
requiring the same configuration **and manifest digest**. Existing science
remains the image selected before validation. Neither request substitutes a new
runtime after tests; publication never rebuilds. `validated-images` and
`release-latencies` Actions artifacts retain provenance and timing data.

Before using the flow, apply the updated bootstrap or these narrowly scoped grants:

```sh
for repository in groundwork groundwork-runtime; do
  gcloud artifacts repositories add-iam-policy-binding "$repository" \
    --project cicl-487801 --location us-central1 \
    --member serviceAccount:groundwork-ci@cicl-487801.iam.gserviceaccount.com \
    --role roles/artifactregistry.reader
done
gcloud storage buckets add-iam-policy-binding gs://cicl-487801-groundwork-build-source \
  --member serviceAccount:groundwork-build@cicl-487801.iam.gserviceaccount.com \
  --role roles/storage.objectCreator \
  --condition="expression=resource.name.startsWith('projects/_/buckets/cicl-487801-groundwork-build-source/objects/ci-image-builds/'),title=groundwork-ci-images"
```

If PR #32's CI Writer grants were previously applied, remove those two Writer
bindings after adding Reader; adding Reader alone does not revoke Writer.
Science tags remain immutable. PR jobs never authenticate or publish. Build
manifests use a dedicated source-bucket prefix, separate from scientific evidence
and catalogue objects. Configure candidate/cache retention for the mutable
repository and manifest prefix according to the desired diagnostic window;
do not apply cleanup to published science images referenced by locked plans.

For a plain local web build after installing a new game, regenerate the same
route artifact before building:

```sh
uv run python scripts/export_environment_ids.py
cd web
npm run build
```

Production deployments run from `main`. For first verification before merge,
temporarily add the chosen branch to the workflow's push trigger and pass
`--verification-branch codex/platform-deployment` to trust exactly that branch.
A new manual workflow cannot be dispatched before it exists on the default branch.
Remove the temporary push trigger before merge and rerun the configuration command
without that flag afterward to remove the temporary trust. Protect `main` with required
reviews and CI checks appropriate to the repository's contributor model.

Cloud Build uses `groundwork-build` and Cloud Logging. A manual platform workflow
run defaults to `publish_only: true`, recording candidate build, GitHub testing
and Cloud Build promotion without updating the hosted service. Verification
branches always use this mode. A normal main push deploys after passing checks.
To repeat the comparison, run the same commit twice and retain the
`release-latencies` artifacts; compare cache population with same-commit warm
runs separately. [CI comparison](../docs/CI_COMPARISON.md) describes the metrics.

For a checked-out, already validated revision, restore that run's `candidates.json`
and `validated-images.json` into `.platform-build/`, then submit using the
checked helper, which rejects a mismatched/unsealed record:

```sh
python deploy/latency.py submit publish --project cicl-487801 --region us-central1 \
  --source-sha FULL_COMMIT --runtime-source-sha SCIENCE_COMMIT --publish-only
```

Omit `--publish-only` only for an authorized hosted release and supply
`GOOGLE_OAUTH_CLIENT_ID`. Do not fabricate validation records to skip tests.

Workload Identity Federation permits impersonation of `groundwork-ci`; API
permissions still come from that service account's roles. Bootstrap grants CI
Cloud Build submission, use of the build identity, access to its source bucket,
repository-scoped Artifact Registry Reader on `groundwork` and
`groundwork-runtime`, and `roles/serviceusage.serviceUsageConsumer`. Its source-bucket access includes
bucket metadata read (`roles/storage.legacyBucketReader`) in addition to object
upload: `gcloud builds submit` checks bucket existence before uploading source.
Both CI and the build identity
require that last role to consume enabled project APIs; a successful OIDC
exchange alone does not prove that a build can be submitted. Provider-secret
metadata access is granted to the build identity on each configured secret,
while secret value access belongs to the worker only.

Verify that unauthenticated private requests fail, two experimenters cannot read
each other's metadata or exports, a scripted hosted launch completes and persists
its artifacts, and a GitHub workflow records a successful Cloud Build deployment.
Runtime and worker image digests and source SHAs are recorded separately in launch
metadata and exports. Retain pinned runtime images and their release metadata;
publish new worker/runtime bindings rather than rewriting old launch records or
release images. Historical Jobs may still be needed by executions already in
flight when a new binding is deployed.

```sh
python deploy/verify_hosted.py --project cicl-487801 --source-sha FULL_COMMIT
```

CI runs this health, Google login configuration, and unauthenticated-denial check
after publication. Real Google login and completed experiment/export checks still
require an authenticated end-to-end run.

Before marking a deployment verified, record its commit SHA, immutable image
digest, successful GitHub/Cloud Build run links, Google login result, preserved
design-editor workflow, completed hosted episode IDs, and downloaded export
checksum verification. Local checks provide independent evidence but do not
substitute for hosted execution. Current verification evidence is recorded by
the deployment task as those checks complete.

## Current limits

Local colocation shares CPU/memory and cannot isolate untrusted agent code. Hosted
execution is isolated per launch, not per seat. Large launches remain bound to one
Job task and its one-hour duration; split them into explicit launches before
increasing scale. External human/agent callbacks and direct per-seat hosting are
future work. A future scheduler can shard immutable plans across Job tasks while
preserving attempt IDs. Owner-based access is implemented by the backend; there is
no cross-user leaderboard or collaboration sharing in this initial release.
Authored `local_process` rosters currently support Calendar baseline scripted
agents; other registered games use `in_process` rosters, as enforced by the
existing design compiler.

The two hosted containers are trusted repository code. They share the Job service
account, execution volume and network; the worker/runtime split assigns operational
responsibility and preserves runtime pins, but provides no credential or IAM
isolation between them. User-supplied images and untrusted agents are unsupported.
