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

Measured head: `e309c09`. Later documentation/timing/cancellation follow-ups do
not change the successful build/test/publish path. The permanent
[JSON snapshot](ci-latencies/2026-10-19-cloud-build.json) retains stages, cloud
steps, queue times, run IDs, and the failed trials. Both ordinary workflows
passed without retries at this head.

| Successful run / stage | Measured time | Conditions |
|---|---:|---|
| [General PR CI](https://github.com/devYaoYH/Project-Groundwork/actions/runs/39229207712) | **3m39s** | Web last, 3m34s; all shards pass |
| [Platform PR validation](https://github.com/devYaoYH/Project-Groundwork/actions/runs/39229207637) | **2m42s** through gate | Credential-free local builds; reporting follows gate |
| [Publish-only pipeline](https://github.com/devYaoYH/Project-Groundwork/actions/runs/39229202779) | **8m01s** through publication | Warm registry caches, changed platform source, existing science; initial wait 4s |
| Candidate request wall time | 3m09.743s | Submission 4.509s; cloud execution 2m59.123s; cloud queue 0.573s |
| GitHub artifact download | 40.839s | Both selected image digests and build manifest |
| Compose startup + verification | 18.199s | 6.540s + 11.659s |
| Platform-image pytest | 1m45.026s | Same suite also runs independently on the host |
| Publish request wall time | 1m13.649s | Cloud execution 1m05.165s; cloud queue 0.523s |

The [identical-commit repeat](https://github.com/devYaoYH/Project-Groundwork/actions/runs/39229734850)
retains its own artifact. Its final figures are recorded in
[PR #33's description](https://github.com/devYaoYH/Project-Groundwork/pull/33),
so recording measurements does not continually change the benchmarked checkout.

These release figures do not prove that #33 is faster than #32: #32's trusted
release path has not been measured under the same conditions. PR checks and a
publish-only release perform different work. The new flow demonstrably reuses
validated artifacts and centralizes release builds/caches; extra worker startup
and artifact transport remain real costs. Publish-only still pulls the catalogue
step's platform image even though its command skips catalogue publication.

### Failures retained during verification

- [Initial CA failure](https://github.com/devYaoYH/Project-Groundwork/actions/runs/39227306660):
  57.739s candidate request; fixed by passing the SDK CA bundle to Docker/Buildx.
- [Cold build / copy failure](https://github.com/devYaoYH/Project-Groundwork/actions/runs/39227551020):
  candidate request 3m49.806s, download 54.528s, platform-image tests 1m44.630s,
  new-science smoke 7.730s. Validation passed, but Docker pull/tag/push changed
  the manifest digest while preserving the configuration. Publication rejected
  activation after 1m48.571s. Its copied science tag for `8e90b19` remains;
  subsequent runs selected and revalidated that existing digest before testing.
- The obsolete repeat `39228127602` was cancelled; its already-submitted
  candidate build finished without validation/publication. A best-effort
  candidate-build cancellation step now handles interrupted polling.
- A strengthened Linux registry fixture rewrites otherwise valid manifest JSON
  to reproduce repacking, then verifies raw promotion preserves its exact digest.
  [Cloud probe `5aa28925`](https://console.cloud.google.com/cloud-build/builds;region=us-central1/5aa28925-4221-40f8-9b8a-474da989025b?project=cicl-487801)
  also passed against the original BuildKit science candidate (`6e55d01b…`). It
  used an isolated mutable package that was removed afterward. The earlier probe
  `08a0a027` passed its copy assertion but failed automatic deletion under Writer
  permissions; administrator cleanup completed without widening the build role.

No hosted rollout was performed. Benchmark-only registry Reader, manifest-upload,
and exact-branch manual WIF grants are temporary and restored after the runs.

### What the measurements suggest next

The general PR path still ends with web (3m34s). The release path spends substantial
time starting fresh cloud workers, materializing cached layers, downloading images,
and running platform pytest after the build. For the changed-source warm build,
Buildx startup was 7.3s; source install plus the two metadata exports consumed
about 33s; cache export itself was 2.5s. Cloud request queues were below one second.
These observations do not point to excessive cache-writer parallelism as the
remaining release bottleneck.

A useful next comparison is a larger Cloud Build worker or a combined tools image,
followed by assessing the intentional overlap between host and candidate-image
platform pytest. Neither change is included here. Browser parallelism remains a
separate potential improvement to PR feedback time.

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
