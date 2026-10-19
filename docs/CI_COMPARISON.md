# CI implementation comparison: PR #32 and PR #33

Both alternatives target `main` and retain the sharding, CI-tooling layout from
`8e90b19`, shared builders, dependency layers, and single cache writers. PR #33
starts from PR #32's `4660bc3`; PR #32 remains unchanged for comparison. They
are alternatives and should not be merged together without reconciling the
release pipeline. Browser parallelism is intentionally unchanged.

| Release stage | PR #32 | PR #33 |
|---|---|---|
| Select old immutable science | GitHub, before tests | Cloud Build, before candidate builds |
| Build new release images/cache | GitHub Bake, GHA cache | Cloud Build Bake, registry `mode=max` cache |
| Transfer before GitHub testing | Local image load | Download manifest + pull candidate digests |
| Test selected pair | GitHub Compose + image tests | Same GitHub checks |
| Publish after gate | GitHub pushes tested configs | Cloud Build promotes tested digests |
| Hosted rollout | Cloud Build, no rebuild | Second Cloud Build request, no rebuild |
| CI registry permission | Writer | Reader |

PR checks still use credential-free GitHub builds in both alternatives. Therefore
ordinary PR run times measure shared PR checks and instrumentation overhead;
they **do not** benchmark the new trusted release path. A publish-only run measures
candidate build → GitHub validation → Cloud Build promotion without hosted rollout.
It still creates cache/candidate artifacts and can publish an immutable science
tag after successful validation. Non-main verification branches cannot deploy.

## Prior measured reference

The pre-optimization PR required 10m32s overall; Python execution alone was
8m47s. These historical measurements are not a controlled cloud comparison.

| Reference | Observable time | Conditions |
|---|---:|---|
| Earlier sharded PR run `39217019203` | 5m11s | Before shared image builders |
| Shared-builder repeat, exact `fc4db4f` commit | 3m45s | Warm same-commit cache; web finished last at 3m41s |
| PR #32 platform run `39222673124` | 2m24s | Credential-free PR validation; stack job 1m57s |

The first shared-builder revision took 5m47s including an 83s initial runner
queue; its active span was about 4m24s. Queue and cache differences matter.
The final #32 general CI attempt mixed unchanged successful jobs with a targeted
retry of a known runtime smoke timeout; do not derive an overall benchmark from
that mixture.

## New measurements

Measurements are added here with exact commit/run IDs after verification. An
unexercised cloud stage is pending, not a measured improvement.

## How to compare

Run the same revision twice with manual platform `publish_only: true`, preserving
both run IDs and `release-latencies` artifacts. Record whether the science source
already exists and whether the registry caches were populated. A second run can
be warm without having identical behavior if the first published new science.

The artifact includes exact-attempt GitHub job queues and steps, Cloud Build
submission wall time (source packing/upload/API included), cloud queue/execution
and per-step image pulls, GitHub artifact download, Compose startup/verification,
platform-image tests, and science smoke when applicable. Submission wall time
and cloud execution overlap; **do not add all fields together**. The cloud
wrapper's wall time includes polling overhead. End-to-end release time also
includes GitHub scheduling and setup between jobs. A publish-only result does
not include live catalogue publication or hosted rollout/verification.

Compare medians of several equivalent warm runs before attributing small changes
to implementation. Initial cache population is a separate result; account for
image/cache storage and transfer as well as runner time. Changing browser test
parallelism should be considered only after these measurements identify it as a
remaining bottleneck.
