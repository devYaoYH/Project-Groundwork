# CI support

This directory owns the pytest sharding plugin, per-file duration estimates,
the timing-refresh utility, and CI-specific tests. GitHub job definitions live
in `../workflows/`; the cached image action lives in `../actions/build-image/`.

From the repository root, run the tooling checks with:

```bash
uv run python -m pytest .github/ci/tests
```

Refresh measured weights from one successful run's downloaded Python/runtime
JUnit artifacts (see the maintenance instructions before collecting reports):

```bash
python3 .github/ci/refresh_durations.py /tmp/ci-timings
```

Keep scheduling code, its data, and its tests together here. Shared workspace
installation and deployment tooling stay with their respective features.

See [docs/CI.md](../../docs/CI.md) for the design, motivation, shard execution,
duration-weight maintenance, caching, and coverage guarantees.
