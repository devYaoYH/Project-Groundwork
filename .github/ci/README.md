# CI support

This directory owns the pytest sharding plugin, per-file duration estimates,
and CI-specific tests. GitHub job definitions live in `../workflows/`; the
cached image action lives in `../actions/build-image/`.

From the repository root, run the tooling checks with:

```bash
uv run python -m pytest .github/ci/tests
```

Keep scheduling code, its data, and its tests together here. Shared workspace
installation and deployment tooling stay with their respective features.

See [docs/CI.md](../../docs/CI.md) for the design, motivation, shard execution,
duration-weight maintenance, caching, and coverage guarantees.
