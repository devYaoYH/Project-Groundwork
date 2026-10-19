# GitHub Actions testing and feedback time

This document records how the repository's checks are installed, scheduled,
executed, and maintained. GitHub Actions workflows are the executable source of
truth. Ordinary verification uses scripted agents and deterministic tests;
provider access and hosted deployment are separate workflows with explicit
configuration.

## Motivation and change history

The October 19, 2026 baseline PR waited 10m32s for CI. Python tests consumed
8m47s inside a 10m29s job; the container gate took 7m13s and platform validation
took 5m36s. Those jobs already ran concurrently: the main opportunities were
serial work within jobs, repeated image builds and dependency installation,
and expensive test setup unrelated to the assertion being checked.

PR #32 introduced these changes:

- `8a29b5d`: independent packaging and platform checks, four Python shards,
  three container shards, cached image builds, and dependency-layer separation.
- `4c731ba`: duration weights from a passing Linux run and bounded stochastic
  fixture work, including removal of incidental provider discovery in tests.
- `8e90b19`: CI sharding code, weights, and tests moved together into
  `.github/ci/`; the shared workspace installer stayed under `scripts/`.
- `fc4db4f`: one Bake builder per image job and one designated cache writer
  per image scope.
- The maintenance follow-up adds the colocated JUnit refresh utility and
  derives shard denominators from the Actions matrix instead of duplicating
  its size in pytest commands.

Four Python runners and three container runners are initial tuning choices.
They balance substantial test execution against repeated installation, image
loading, runner startup, and queue time. A PR below five minutes is an objective,
not a guaranteed benchmark. Compare cold and warm cache runs separately.

## File ownership

```text
.github/
  workflows/
    ci.yml                        # general PR/main checks and shard matrices
    platform.yml                  # platform tests, Compose verification, deployment
  actions/build-image/
    action.yml                    # reusable cached build/load action
    docker-bake.hcl                # paired targets, tags, cache scopes/write policy
  ci/
    README.md                     # short entry point and pointer to this document
    ci_sharding.py                # pytest collection and file scheduling
    ci_durations.json             # repository-relative file costs in seconds
    refresh_durations.py           # validated JUnit-to-weight refresh
    tests/
      conftest.py                 # import path for colocated tooling
      test_ci.py                  # coverage, image reuse, and workflow invariants
      test_refresh_durations.py   # report parsing and refresh safeguards
scripts/
  install_workspace.py            # shared installation/dependency discovery
  workspace.py                    # test, smoke, wheel, and export commands
  ...
tests/
  workspace/test_install_workspace.py
  runtime/compose_support.py      # real container test image-reuse adapter
  platform/test_deployment_release.py
  platform/test_validated_images.py
deploy/
  validated_images.py             # tested image IDs, publication, digest-only handoff
Dockerfile                        # original viewer and runner
Dockerfile.platform               # control API/UI and infrastructure worker
Dockerfile.runtime                # independently pinned scientific runtime
a2a-agent/runtime/Dockerfile       # isolated agent service
```

Keep CI-only scheduling assets and their tests together in `.github/ci/`.
Keep the composite image action in its Actions directory. The installer belongs
to workspace tooling because local development, package discovery, and all four
Dockerfiles use it. Runtime helpers and deployment tests remain with the features
they verify. Full CI documentation lives here; `.github/ci/README.md` stays brief.

## Workflow triggers, isolation, and required gates

`ci.yml` runs on every pull request and on pushes to `main`. `platform.yml` runs
on PRs touching its declared platform/runtime/web/build paths, on pushes to
`main`, and through manual dispatch. Changes to reusable image actions are
included in the platform PR path filter. Workflow permissions default to
`contents: read`.

Both workflows cancel superseded runs of the same PR. Their cancellation policy
does not cancel an in-progress main-branch run. Matrix jobs use
`fail-fast: false`: a failed shard leaves the other shards running so their
results and timing reports remain available.

Each job, including each matrix shard, receives its own `ubuntu-latest` runner.
This isolates files, processes, Compose projects, ports, and mutable Docker tags.
Steps within one job execute sequentially; independent jobs run concurrently,
subject to GitHub runner availability and account limits.

| Workflow/job | Work performed | Parallelization/dependencies |
| --- | --- | --- |
| CI `lint` | Ruff lint and format checks | Independent |
| CI `web` | Install workspace, export environment/site metadata, npm tests, TypeScript checking, production build, Chromium browser tests | Independent; steps sequential |
| CI `python-tests` | Registered package, local-stack, workspace, and CI-tooling pytest collection | Four matrix runners |
| CI `package-tests` | Workspace smoke, turn-contract checks, packaged MCP/runtime smoke, release/Compose validation, wheels and installed-wheel exports | Independent of pytest shards |
| CI `test` | Required gate for `python-tests` and `package-tests` | Waits for both |
| CI `runtime-tests` | Real game, transport, CLI-launch, and viewer containers | Three matrix runners |
| CI `runtime-containers` | Required gate for all runtime shards | Waits for `runtime-tests` |
| CI `local-stack` | Viewer/Redis/SQLite integration, design-lock launches, persisted replay/metrics/API assertions | Independent |
| Platform `platform-tests` | `cloud_platform/tests` and `tests/platform` | Independent of Compose job |
| Platform `platform-stack` | Build paired platform/science images and run `deploy/verify_local.py` against the live stack | Independent of host pytest |
| Platform `validate` | Required gate for platform pytest and Compose verification | Waits for both |

The aggregate checks retain the existing branch-protection names: `test`,
`runtime-containers`, and platform `validate`. Each uses `if: always()` and
explicitly requires `success` from every dependency. Failed, cancelled, or
skipped children cannot turn into a successful gate. Other directly required
checks still depend on the repository's branch-protection settings.

The platform deployment job depends on both `validate` and the image outputs
from `platform-stack`, excludes PR events, and requires configured workload
identity. PR validation never authenticates to GCP or publishes images.

For a trusted release, `platform-stack` resolves the immutable science source tag
before building. If it exists, the job pulls its digest and builds only the new
control image; otherwise it builds both images. Compose verification therefore
uses the same science image the release will deploy. Record both local image
configuration IDs before testing and verify them again afterward. Trusted release
runs also execute platform pytest in the built image and the packaged science
smoke for newly built runtimes, preserving the previous Cloud Build checks.
Previously published science images still receive Compose/bridge verification;
the workflow does not assume they contain today's workspace smoke script.

After validation, publish those exact image IDs to Artifact Registry and check
that each registry manifest references its tested configuration ID. A conflicting
science source tag fails rather than replacing a pin or silently selecting an
untested image. Platform publication tags include the source, run ID, and attempt;
deployment receives immutable digests and the validated source SHA through job
outputs. The `validated-images` artifact records the local IDs, source pins,
validation status, and published digests for inspection.

`cloudbuild.yaml` is deploy-only: reject source mismatches, mutable references,
or the wrong registry namespace; publish the catalogue from the selected platform
image; deploy both digests; then verify the hosted release and runtime bindings.
It never builds, substitutes, or retests a different image. The separate
`deploy/cloudbuild-image.yaml` remains an initial-bootstrap build-only path.
This reuse removes a rebuild cycle after merge; it does not promote PR artifacts
across a potentially different merge revision.

Before the first release with this flow, apply the updated `deploy/bootstrap.py`
or its documented repository-scoped CI writer grants. WIF remains restricted to
the trusted workflow/branches and push/manual events. This PR does not apply live
IAM changes or deploy to GCP. See `deploy/README.md` for the rollout commands.

## Host test environment and discovery

Python jobs use `astral-sh/setup-uv`, Python 3.12, a fresh virtual environment,
and uv's download cache. They install pytest and any job-specific dependencies
before running `scripts/install_workspace.py`. That installer discovers the
core packages and game packages registered through the
`a2a_engine.environments` entry point. It installs local packages in editable
mode and uses uv when available, with a pip fallback for developer hosts.
Platform pytest additionally installs `requirements-platform.txt`.

`python scripts/workspace.py test` invokes pytest in importlib mode over every
registered package root, `local_stack/tests`, `tests/workspace`, and explicitly
`.github/ci/tests`. Recursing from package roots covers Calendar's two test
layouts. Explicitly listing the hidden CI directory prevents ordinary pytest
recursion from omitting those tests. Platform and real-container suites have
their own jobs and commands.

The web job uses Node 22, `npm ci`, and npm's download cache keyed from
`web/package-lock.json`. It generates workspace/environment metadata before
building the UI, then installs Chromium and runs the browser suite. These
checks cover separate contracts; a passing Python suite does not replace them.

## Dynamic file sharding

Both sharded jobs set `PYTHONPATH=.github/ci` and load pytest with
`-p ci_sharding --ci-shard=index/count`. Each runner collects its entire suite
before the plugin selects its assigned files:

1. Count collected tests per repository-relative file.
2. Read `ci_durations.json` beside the plugin. Known files use their measured
   seconds; unknown files use `max(1, collected_test_count * 0.1)`.
3. Sort files by descending estimated cost, breaking ties by path.
4. Assign each file to the currently least-loaded shard, breaking ties by index.
5. Deselect files belonging to other shards, then execute the selected tests.

All runners use the same checkout, weights, collection, and shard count, so the
assignment is deterministic and covers each collected file exactly once.
Files stay intact to preserve module fixtures and avoid repeating file setup
across runners. The log prints the selected test count and estimated file loads.
New tests and registered games enter discovery automatically. Weights influence
placement only; they never declare which tests exist. Removed-file weights are
ignored by scheduling.

The matrix arrays in `ci.yml` are the sole source of runner counts:

```yaml
matrix:
  shard: [1, 2, 3, 4]
# In the test command:
# --ci-shard=${{ matrix.shard }}/${{ strategy.job-total }}
```

The container matrix currently lists `[1, 2, 3]`. `strategy.job-total` supplies
the denominator automatically. Keep shard indices contiguous and one-based.
Do not add another matrix dimension or include/exclude entries without updating
the scheduling design: `job-total` counts all expanded jobs. CI tooling tests
check this single-dimension, contiguous-index contract and the command wiring.

More runners will not split a single dominant file. If one file controls the
critical path, first examine its fixture/setup cost and test responsibilities;
consider splitting independent scenarios into separate files while preserving
coverage and fixtures. Adding runners also repeats checkout, collection,
installation, and image loading and can increase queues or cache contention.

## Cached Docker image construction

`.github/actions/build-image/action.yml` consolidates the GitHub build logic.
Callers select `targets` and optionally `cache-write` (default `false`). The
colocated `docker-bake.hcl` owns image Dockerfiles, local tags, cache scopes,
and groups. The action starts one Buildx builder and invokes
`docker/bake-action` once. The `runtime` group builds viewer and agent together;
the `platform` group builds control and science together. Bake can build the
pair concurrently and reuse common dependency work within the shared builder.
The local-stack job selects the single `viewer` target.

Every target imports GitHub Actions BuildKit layers from its own scope.
`CI_CACHE_WRITE` is passed from the action input into Bake; only when it equals
`"true"` does the target export layers with `mode=max`, retaining intermediate
stages for later runs. `load: true` loads the current-source images into that
runner's Docker daemon under the tags Compose expects. The action does not push
finished images to a registry.

| Target / scope | Dockerfile | Local tag | Consumers / designated writer |
| --- | --- | --- | --- |
| `viewer` / `viewer` | `Dockerfile` | `a2a-comm-local:latest` | Runtime shards and local-stack; runtime shard 1 writes |
| `agent` / `agent` | `a2a-agent/runtime/Dockerfile` | `a2a-agent-runtime:local` | Runtime shards; runtime shard 1 writes |
| `control` / `platform` | `Dockerfile.platform` | `groundwork-platform:local` | Platform-stack reads and writes |
| `science` / `science` | `Dockerfile.runtime` | `groundwork-runtime:local` | Platform-stack reads and writes |

In `ci.yml`, `cache-write: ${{ matrix.shard == 1 }}` makes runtime shard 1 the
viewer/agent writer. The other runtime shards and local-stack only import these
scopes. In `platform.yml`, platform-stack sets `cache-write: "true"` for its
separate pair. This removes redundant cache exports from sibling jobs; it does
not serialize jobs behind the writer. Shard 1 was selected to put export work
on a relatively short test shard. Recheck that choice when timings or shard
counts change; the writer still builds all images before running its tests.

Each runner still builds and loads its own images. One builder is shared within
an image job, not across separate GitHub runners. Readers may start before the
writer finishes and use earlier caches or build missing layers themselves.
A cache miss rebuilds layers from current inputs. Inspect build logs to
distinguish misses from cache import/export failures. Cache visibility and
eviction also affect hit rates, and separate workflow runs can still overlap
when exporting a scope; the single-writer policy applies within a run.

All four Dockerfiles separate external dependencies from source installation:

```text
requirements stage: package declarations → external dependency constraints
                               ↓
dependencies stage: uv installs third-party packages
                               ↓
source stage: copy current source → editable workspace install with --no-deps
```

`scripts/install_workspace.py --requirements-output ...` retains dependency
constraints and excludes packages provided by the workspace itself. The platform
image also installs its platform requirements. The requirements stages currently
copy complete `games/` trees to discover registered packages: a game source edit
can rerun requirements export, while an unchanged generated requirements file
allows downstream dependency layers to remain cached. Other source changes need
not reinstall external dependencies. Source installation still runs against the
current checkout. These exported constraints are not a dependency lockfile.

The viewer/platform web stages copy npm manifests and run `npm ci` before
copying web source, preserving their npm installation layer on source-only
changes. Web builds still run when their inputs change. The science runtime's
source stage includes scientific packages and inputs and excludes the control
API, infrastructure worker, and web application.

The action finishes with `docker buildx use default`. Fixture Dockerfiles inherit
the agent image through its local tag, so subsequent Compose builds need the
builder that can resolve those loaded local images.

## Reusing images without reducing container coverage

Runtime shards build/load viewer and agent images before pytest and tag the
viewer as `a2a-runtime-launch-test:local` for launch tests. They set
`GROUNDWORK_CI_PREBUILT_IMAGES=1`. The runtime Compose helper then:

- Removes redundant base-service builds for `runtime-environment` and the
  ordinary `runtime-agent`.
- Still builds specialized agent fixtures when the test requests one.
- Replaces `up --build` with `up --no-build`.
- Leaves execution, assertions, timeouts, persisted readback, and teardown intact.

The local-stack and platform-stack jobs also build images explicitly before
starting Compose with `--no-build`. This keeps the image being tested tied to
the checkout and avoids implicit repeated builds during startup.

Without the prebuilt flag, local runtime tests retain their normal build
behavior. Do not set the flag against stale images. It is an optimization for a
job that has already built the current sources, not a general test-mode switch.

The local-stack job verifies real SQLite/Redis evidence and viewer projections;
platform verification checks the worker/science bridge, execution completion,
exports, and ownership boundaries. `test_deployment_release.py` separately
checks release pinning, immutable catalogue publication, build/deployment
ordering, source selection, and credential boundaries using command/HTTP mocks
and configuration assertions. Those tests complement live Compose verification.

## Test-cost reductions and evidence

Calendar ordering tests skip unrelated optimal-schedule computation. Negotiation
retains a full 1,000-pass integration regression and cheap checks of nonpositive
input values at the 999/1,000 boundary and graph execution-budget calculation.
The scenario-reproducibility test uses the real solver/oracle with a fixed seed
and a bounded iteration budget. Optional project naming is stubbed so ordinary
tests do not perform ADC discovery or provider calls.

These changes reduce incidental or repeated work while retaining transport,
protocol, privacy, cleanup, packaging, and scientific regression coverage.
Use scripted smoke runs for ordinary verification; `--dry-run` can contact model
providers and is not a replacement for `--smoke-test`.

## Timings and maintenance

Python shards upload `python-timings-N`, container shards upload
`runtime-timings-N`, platform pytest uploads `platform-timings`, and the web job
uploads `browser-timings`. Upload steps use `if: always()` so failures can also
be investigated; an uploaded report alone does not establish success. JUnit
pytest times include setup/call/teardown under pytest's default total-duration
setting. They exclude runner queueing, installation, image builds, and other
workflow steps. Use the Actions job/step timeline for those costs.

### Refresh duration weights

Refresh after significant test/fixture changes, added or moved files, or
persistent imbalance between shards. The initial weights came from a passing
Linux PR #32 run, with the bounded solver's focused timing replacing its old
90-second measurement. Refresh is deliberate and reviewed; CI never rewrites
tracked weights automatically.

Use all Python and runtime artifacts from **one successful CI run**, ideally on
an otherwise idle runner fleet, with the corresponding source revision checked
out. Failed, interrupted, skipped, or mixed-run results make poor scheduling
estimates. Confirm every expected shard finished before refreshing; the utility
cannot infer missing artifacts from a directory. Do not mix browser or platform
reports into this update: those suites are not scheduled by this plugin.

From the repository root, with an authenticated GitHub CLI:

```bash
# Find and inspect a successful run for the branch/revision being measured.
gh run list --workflow ci.yml --branch YOUR_BRANCH --limit 10
run_id=YOUR_SUCCESSFUL_RUN_ID
gh run view "$run_id"

# A fresh directory prevents accidental mixing with older runs.
timing_dir="$(mktemp -d)"
gh run download "$run_id" --pattern 'python-timings-*' --dir "$timing_dir"
gh run download "$run_id" --pattern 'runtime-timings-*' --dir "$timing_dir"

# Defaults to updating .github/ci/ci_durations.json.
python3 .github/ci/refresh_durations.py "$timing_dir"
git diff -- .github/ci/ci_durations.json
```

The utility accepts XML files or artifact directories, resolves pytest xunit2
classnames (including nested test classes) to checkout files, and sums testcase
seconds per file. Explicit JUnit file attributes are also supported. It replaces
weights for measured files and preserves unmeasured entries. Duplicate cases
across reports, failed/error/skipped cases, empty reports, unknown source files,
and negative/nonfinite/missing durations cause an error before the output is
written. Pass a fresh `--output /tmp/ci-durations-preview.json` to inspect a
measurement-only preview without changing the tracked file. Zero durations are
valid at JUnit's millisecond precision.

Review unusually large changes against the test and step logs. Remove obsolete
paths deliberately when files move or disappear; preserved old entries are
harmless but should not accumulate indefinitely. Commit the JSON update with
the source/fixture change or a focused maintenance change, recording the run ID
and relevant timing observations in the PR. Re-run tooling checks and compare
actual shard elapsed times on the next CI run.

### Decide when to change runner counts

Inspect several comparable successful runs before changing a matrix. Watch:

- The slowest required job and end-to-end PR time, including queueing.
- Spread between shard execution times versus their estimated file loads.
- A single dominant file, setup overhead, or stochastic variation.
- Image-build cache hits, dependency-install time, and artifact/cache transfer.
- Runner availability, repeated setup costs, and cache-export time on the
  designated writer. Recheck runtime shard 1's load after rebalancing.

Refresh stale weights before adding shards. If work is balanced and test
execution still dominates, change only the relevant `matrix.shard` array;
the denominator follows automatically. If setup or image loading dominates,
more shards can cost more without shortening the PR. Validate coverage
conservation and compare warm/cold results after any change. Preserve the
aggregate required-check names and their fail-closed behavior.

## Local reproduction and validation

Install host tooling as described in [INSTALLATION.md](INSTALLATION.md). From
the repository root:

```bash
# Focused CI/workspace tooling checks.
uv run python -m pytest .github/ci/tests tests/workspace

# All discovered workspace tests, without sharding.
uv run python scripts/workspace.py test

# Reproduce one Python shard (use the current matrix size).
PYTHONPATH=.github/ci uv run python scripts/workspace.py test \
  -p ci_sharding --ci-shard=1/4 --durations=30

# Inspect assignment without executing tests.
PYTHONPATH=.github/ci uv run python scripts/workspace.py test \
  -p ci_sharding --ci-shard=1/4 --collect-only -q

# Real container suite: Docker required; ordinary local builds enabled.
uv run python -m pytest tests/runtime -q --durations=20

# Independent platform host suite (install requirements-platform.txt first).
uv run python -m pytest cloud_platform/tests tests/platform -q

# Repository formatting/lint scope; includes the colocated CI tooling.
uvx --from ruff==0.15.2 ruff check .
uvx --from ruff==0.15.2 ruff format --check .
```

The tooling tests verify deterministic exactly-once file assignment, automatic
new-file scheduling, the matrix/denominator contract, required-gate dependencies,
fixture execution under prebuilt-image reuse, and timing-refresh validation.
For scheduling changes, also collect the full suite and every shard and compare
node-ID unions and counts: every full-suite item must appear exactly once.
Validate workflow edits with actionlint when available. Local tooling checks
do not establish cache performance or replace the live GitHub/Compose checks.
