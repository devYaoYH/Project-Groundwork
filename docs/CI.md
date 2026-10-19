# CI feedback time

The October 19 baseline PR waited 10m32s for CI. Python tests consumed 8m47s
inside a 10m29s job; the container gate took 7m13s and platform validation
took 5m36s. These jobs already ran concurrently.

CI now uses four Python shards and three container shards on separate runners.
Files remain intact within a shard, preserving module fixtures and avoiding
concurrent checkout mutations or mutable Docker image tags on one runner.
Discovery still collects all registered packages, including newly added games.
`.github/ci/ci_sharding.py` assigns every collected file exactly once using
`.github/ci/ci_durations.json`; unknown files receive a test-count estimate.
Weights come from the first passing Linux run of PR #32. Refresh them from
CI artifacts after significant test changes; the bounded solver case uses its
new focused timing instead of its previous 90-second duration.
Weights only influence scheduling; they never determine which tests exist.

The `test`, `runtime-containers`, and platform `validate` checks aggregate all
their children and fail on failed, cancelled, or skipped dependencies. Existing
branch protection names remain valid. Packaging/smoke checks and platform
pytest/Compose checks run independently. Superseded PR runs are cancelled;
running main-branch verification and deployments are never cancelled by this policy.

Image builds import/export BuildKit caches by image scope. A metadata stage
exports external dependency constraints from trusted package declarations;
source-only changes retain the dependency layer. Editable source packages are
then installed with uv and `--no-deps`. The pip installer remains available on
hosts without uv. The science runtime still excludes control, worker, and web
code, and existing immutable runtime images are unaffected.

Container shards build/load the current viewer and agent images first. Only
those base builds are reused; fixture images, real container execution,
privacy assertions, deadlines, persisted readback, and teardown still run.
Without `GROUNDWORK_CI_PREBUILT_IMAGES=1`, local container tests retain their
ordinary build behavior. Do not set it against stale images.

Reproduce a Python shard after the installation in INSTALLATION.md:

```bash
PYTHONPATH=.github/ci uv run python scripts/workspace.py test -p ci_sharding --ci-shard=1/4 --durations=30
```

For a full unsharded run, omit the plugin and shard arguments. Every Python,
container, and platform test job uploads JUnit timings, including failures.
Use those reports to refresh file weights and compare cold and warm cache
runs; a sub-five-minute PR target is an objective, not a guaranteed benchmark.

Full transport/protocol parity and packaging smoke coverage remain required.
Calendar ordering tests skip unrelated optimal-schedule computation. Negotiation
retains a full 1,000-pass integration regression and tests both nonpositive
input values at the 999/1,000 boundary and graph execution-budget calculation.

The scenario-reproducibility test still executes the real Negotiation solver and
oracle with a fixed random seed and a bounded iteration budget. Optional project
naming is stubbed so ordinary tests do not perform ADC discovery or provider calls.


## File ownership and local tooling checks

CI scheduling code, duration estimates, and their tests live together in
`.github/ci/`. Its README provides a short entry point; this document remains
the detailed guide. Workflow orchestration stays in `.github/workflows/`, and
the reusable image action stays in `.github/actions/build-image/`.

`scripts/install_workspace.py` and its tests remain workspace tooling: local
development, package discovery, and all four Dockerfiles use the installer.
Deployment correctness tests remain in `tests/platform/`.

Run the CI tooling and workspace tests from the repository root:

```bash
uv run python -m pytest .github/ci/tests tests/workspace
```

`scripts/workspace.py test` explicitly collects `.github/ci/tests`, including
in an unsharded run. The sharded jobs set `PYTHONPATH=.github/ci` before loading
`-p ci_sharding`; the duration JSON is resolved relative to the plugin file.
New test files still enter the collection without edits to the weights.
