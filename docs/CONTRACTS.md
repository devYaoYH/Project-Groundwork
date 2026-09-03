# Contracts

The interfaces that make a new release reproducible by construction.
If you are adding a environment, read this first, then `ADDING_A_GAME.md`.

---

## 1. Environment contract

A environment is a class the runner can construct and call:

```python
class MyGame:
    def __init__(self, config: dict, dry_run: bool = False) -> None: ...
    def run(self) -> EpisodeTrace: ...
```

Registered at import time:

```python
register_environment(
    "my_game",
    MyGame,
    resolve_config=my_resolver,          # optional
    storage={"backend": "sqlite"},       # optional local-first default
    package="my-environment",                   # optional, for version pinning
    dry_run_checks_keys=True,            # optional
)
```

Discovery is by entry point, so `a2a-run` resolves a environment the caller never
imported:

```toml
[project.entry-points."a2a_engine.environments"]
my_game = "my_game"
```

A cell may override `environment_id`, so one experiment can span several games —
each gets its own `resolve_config` hook at expand time.

### `resolve_config(config) -> dict`

Called **once per cell, before fan-out**. This is where a environment derives config
from config: sampling a task, or running a solver to synthesize a scenario.

Its output is merged into the run config, which the runner records in the trace.
That makes the resolved parameters reproducible — see
`games/negotiation/negotiation_game/resolve.py`, where an `mc_ratio` is expanded
into concrete `agent_projects` by a simulated-annealing solver. If the environment reads
or generates a complete scenario outside the config, it must additionally record
an immutable input artifact (or content hash plus portable source) before it can
claim full replayability.

Because it runs once per cell, every run in a cell shares one scenario. If you
want per-run variation, vary the seed across cells.

### `dry_run_checks_keys`

`--dry-run` means different things to different games:

- **True** (default): assert every LLM agent has a usable API key. Calendar
  relies on this to catch misconfiguration before spending money.
- **False**: the environment substitutes non-LLM stand-ins. Negotiation swaps in
  heuristic agents, so demanding keys would fail a run that needs none.

---

## 2. Release declaration contract

A release bundles pinned code, a hand-written **declaration**, an oracle, and an
item bank. The declaration is the boundary between source code and research
design: it is the only thing a design is validated against, and it is what the
environment screen renders.

```python
register_environment("my_environment", MyGame, declaration=DECLARATION, ...)

ReleaseDeclaration(
    environment_id=..., version=..., blurb=..., source_url=...,
    parameters=[ParameterConfig(name, type, domain, fixed, source, item_key)],
    roles=[RoleConfig(id, count, accepts=["llm" | "scripted" | "human"])],
    item_policy=ItemPolicy(mode="enumerate" | "sample", bank_path, item_bank_sha256),
    measures=[MeasureConfig(name, producer, grain, index_label, unit, direction)],
    oracle_version=...,          # None means this release ships no oracle
)
```

Three rules carry most of the weight:

- **`source` says who supplies a parameter's value.** A `design` parameter is
  written into the episode config; an `item` parameter is frozen in the bank
  beside the oracle result, so a design *selects on* it rather than setting it.
  Hand-writing a `domain` for an item parameter is rejected — the levels are
  projected from the pinned bank at read time, which is what removes the second
  copy that drifts.
- **`fixed` is the release's only veto**, and it is absolute. The declaration
  deliberately does not say whether a parameter *ought* to be an axis: that is a
  claim about one comparison, not a property of the release.
- **A sequence-grained measure names the environment's own `index_label`.** That
  is where the episode viewer's index separators come from. A release that
  declares none gets a continuous lane rather than an invented boundary.

Anti-drift is enforced at check-in, not derived: `games/*/tests/test_declaration.py`
asserts every declared parameter exists on the config class with a compatible
type, and that every item parameter names a column the pinned bank carries.
`scripts/run_game_runtime.py --smoke-test` asserts every declared
`producer="environment"` measure appears in what the episode emitted.

---

## 3. Design contract

A design is a document and **the document is the record**: stored verbatim as
text alongside its `design_sha256`, parsed by a real YAML parser into a strict
model. Locking it is the preregistration; a launch refuses if the text no longer
hashes the same, and editing a locked design forks rather than amends.

```yaml
release: negotiation@v1
parameters:
  mode:       {factor: [stable, rotating]}   # an axis: levels become cells
  num_rounds: {pin: 10}                      # constant across the experiment
  mc_ratio:   {randomize: true}              # item only: varies within a cell
units:  {episodes_per_cell: 8}
roster: [{id: a, kind: llm, binding: gpt-4o-mini}, ...]
seed:   {mode: derived, root: 17}
```

Exactly one disposition per parameter, and the asymmetry is deliberate: a design
parameter is factor-or-pin because the release already has a default for it,
while an item parameter may also be randomized because the bank varies and
something must choose. **An item attribute the pinned bank varies and the design
never dispositions is a compile error**, naming the attribute and the levels the
bank holds — the safe-looking omission is exactly the confound-by-omission the
rule exists to kill.

Compilation is authoritative Python (`a2a_engine/compiler.py`). `web/lib/validate.ts`
mirrors the fast subset for instant feedback while typing and can never cause a
wrong run, because the server re-validates before every launch.

---

## 4. Trace + manifest contract

`EpisodeTrace` is the canonical record: `config`, `events`, `final_state`,
`metrics`, timestamps. Typed runs also persist an `release` reference
(`id`, `release`, canonical content hash, and input hashes) and an `episode`
reference (the concrete experiment/cell/run identity). Alongside it, every run writes a **`EpisodeManifest`**
(`a2a_engine.manifest`) pinning:

| Group | Fields |
|---|---|
| identity | `experiment_name`, `episode_id`, `cell_id`, `episode_idx`, `episode_uid` |
| provenance | `git_hash`, `a2a_engine_version`, `game_package_version`, host, user |
| inputs | `resolved_config_hash`, `seed`, `dataset_path` + `dataset_sha256`, `task_id`, `prompt_variant_id` |
| agents | model, `api_format`, temperature, max_tokens |
| storage | backend, URI, status, error |
| typed release | release ID, version, content SHA-256 |

**The rule this enforces:** anything that changes model behavior must be in the
resolved config, and the resolved config is in the trace.

Two consequences worth stating outright:

- **A dataset is identified by content, not filename.** `dataset_sha256` is a
  hash of the file, because task files get edited in place and a path proves
  nothing.
- **Credentials are excluded from the hash and the file.** API keys are not
  experimental variables; including them would make two collaborators running
  the identical experiment produce different hashes, and would make manifests
  unsafe to upload.

### Message events

Emit natural language as `data: {speaker, text}`. That single convention is what
lets `EpisodeDataset.to_messages_df()` and the shared judge work on any environment
without environment-specific code. Extra fields are preserved — negotiation keeps its
original `message` key alongside the normalized `text`.

---

## 5. Storage contract

```python
class EpisodeStore(Protocol):
    def put_episode(self, trace, manifest) -> str: ...
    def get_episode(self, episode_uid) -> EpisodeTrace | None: ...
    def list_episodes(self, filters, limit, cursor) -> tuple[list[dict], str | None]: ...
    def check(self) -> StoreCheck: ...
```

Optional, used when present: `completed_episode_ids(experiment_name)` powers
`--resume` and the launch-progress join; `iter_episodes(filters)` gives
`EpisodeDataset` a streaming read; `episode_summaries(filters, limit, cursor)`
serves a list view from promoted columns without rehydrating whole traces.

Backends: `sqlite` (the zero-setup default), `local` JSON, `s3`, `firestore`.
Selected per experiment, with inheritance from named sinks:

```yaml
# experiments/storage.yaml
sinks:
  local_db: {backend: sqlite, path: "${A2A_TRACE_DB:-./results/a2a.db}"}

# the experiment
storage:
  extends: local_db
  path: ./results/pilot.db
```

Precedence, lowest first: **environment's registered default → named sink → inline
`storage:` keys → CLI flag.** Storage resolves per experiment, not per cell —
the runner holds one open sink for the whole run, which is what lets a
multi-environment experiment be queried as a single corpus.

Values interpolate `${VAR}` / `${VAR:-fallback}` from the release. A bare
`${VAR}` that is unset raises, rather than passing the literal string on to a
cloud SDK. See `docs/STORAGE.md`.

Invariants every backend must uphold:

1. **Something local is written first and always.** For `local` and `s3` that is
   the JSON tree; for `sqlite` the database file itself is the ground truth
   (`mirror_json: true` adds the JSON tree back).
2. **Remote failure never fails a run.** It is recorded in
   `manifest.storage.status` and logged. This preserves the behavior the
   calendar benchmark already depended on.
3. **`put_episode` returns a URI that actually resolves.** A failed upload returns
   the local path, not the remote one it never reached.
4. **`check()` must not litter shared state.** Local and SQLite do a full
   write/read/delete canary; S3 and Firestore probe read access only, so a
   preflight never leaves debris in a shared bucket or collection for the
   analysis layer to filter out later.

Firestore additionally preserves two behaviors the existing negotiation corpus
depends on: gzip+base64 event compression, and flattening of `list[list[...]]`
(which Firestore rejects) into `{"0": [...], "1": [...]}`, restored on read.

---

### Typed release and experiment YAML (v1)

New environments should use two Pydantic-validated files while legacy
`defaults`/`cells` experiments continue to work unchanged:

```yaml
# environments/my_world_v1.yaml
schema_version: 1
id: my-world.tiny
release: v1
engine:
  environment_id: my_game
  defaults: {num_agents: 2, task_path: games/my-environment/tasks/tiny.jsonl}
inputs:
  - id: task-corpus
    path: ../tasks/tiny.jsonl
    sha256: "<64-character SHA-256>"
roles: [{id: participant, count: 2}]
metrics: [{name: utility, producer: environment, direction: maximize}]
adapter_bindings: {model: engine.llm, communication: local.in_process}
```

```yaml
# experiments/local_smoke.yaml
schema_version: 1
name: my_game_local_smoke
release: ../environments/my_world_v1.yaml
episodes: [{label: baseline, count: 1, seeds: [7]}]
storage: {backend: sqlite, path: ./results/my_game.db}
observability: {capture_content: true}
```

`ReleaseDeclaration` describes the world and pins every declared input by
digest; `ExperimentConfig` selects agents, concrete episode seeds, storage, and
observability. The loader expands this format into the existing runner cells,
so migration is additive. Local relative paths may include `..` when the normal
`environments/`, `experiments/`, and `tasks/` folders are siblings; absolute
paths and cloud URIs are intentionally rejected in v1.

### Item and oracle invariant

`ParameterConfig.source` is an oracle boundary. An item-sourced parameter is
one whose change changes the item's pinned oracle ground truth. Its value and
that ground truth are frozen together in the content-addressed item bank. A
design-sourced parameter cannot affect the pinned oracle result: it may change
episode behavior or a runtime-derived metric, but it must not change an oracle
value stored in a bank row.

Accordingly, an item bank contains only ground truth determined by its frozen
item attributes. Do not store a result that depends on a design parameter in
the bank. Buyer-Seller is the reference case: seller cost, buyer value, item
count, and `discount_factor` are all item-sourced. Every bank row stores the
actual configured-game optimum, `best_joint_utility`, for that complete tuple.
`discount_factor` can be factored, pinned, or randomized only as item selection;
the compiler copies its value from the selected row and never accepts an
independent design-configured value. The bank provides matched rows for the
intended `0.5` and `1.0` delta strata at fixed valuation tuples, so that
comparison remains a legitimate item-stratified experiment.
Buyer-Seller also fixes its protocol horizon in the release, so no remaining
design-open parameter changes this oracle baseline.

Buyer-Seller may also report `undiscounted_total_surplus`. It is an
informational derived quantity, not an oracle optimum and not an item-bank
oracle result.

Callable-oracle perturbation tests are deferred until releases expose a common
callable oracle contract. Buyer-Seller instead checks its concrete bank rows:
the otherwise identical three-unit, surplus-22 rows at discount factors `0.5`
and `1.0` store respective oracle optima `38.5` and `66.0`.

When an release declares roles, an explicit `agents:` list must assign a
declared role to every agent and match each role's declared count. If `agents:`
is omitted, `engine.defaults.num_agents` must match the total role count. This
makes the release/experiment pair a complete, inspectable statement of the
episode topology rather than a best-effort hint.

### Machine-readable schema

The Pydantic models are the normative contract. Export their exact JSON Schema
for editor integration, CI, or external tooling with:

```bash
uv run python scripts/export_config_schemas.py --output-dir schemas
```

This writes `release-config.v1.json` and `experiment-config.v1.json`.
Regenerate them from the installed code rather than maintaining a hand-written
copy; YAML is still loaded and validated by the same Pydantic models at run
time.

Typed experiments write OTel JSONL under `--results-dir/otel-spans.jsonl` by
default. Set `observability.local_spans_path` to pin a different local path;
Compose deliberately supplies its shared `/data/otel-spans.jsonl` projection.

`adapter_bindings` is a durable declaration, not a forced generic transport.
`a2a_engine.adapters` supplies typed model/communication/resource adapter
protocols and a name/kind-scoped registry. Games can migrate one component at a
time while episodes already identify the requested implementation.

### Post-episode metric contract

For `producer: environment`, write the measurement in `EpisodeTrace.metrics` during
the episode. For an expensive or evolving metric, declare
`producer: derived`, a `scope` (`episode` or `participant`), and an extractor
identifier. Implement a typed `DerivedMetricExtractor`, register it from the
environment package, then replay completed episodes:

```python
class MyMetricExtractor:
    identifier = "my-environment.fairness"
    version = "v1"

    def extract(self, trace, artifacts):
        return DerivedMetricResult(values={"fairness": 0.75})

register_derived_metric_extractor(MyMetricExtractor())
```

```bash
python scripts/materialize_metrics.py --database ./results/a2a.db --environment my_game
```

The materializer writes a digest-bound `DerivedArtifact`, never mutates the
source trace, and is idempotent on re-run. The generic viewer fetches and shows
those artifacts beside the trace and OTel spans. Calendar's typed reference
uses `calendar.score_margin` as the worked participant-metric example.

Calendar also registers `calendar.rating.v1` only as a replay compatibility
extractor for pre-v1 local episodes. It preserves that old maximizing
`score_margin` projection as negative excess cost; new environments must use
the v1 `calendar.score_margin` / minimizing `excess_cost` declaration.

---

## 6. Provenance contract

Every episode carries a `provenance` block, stamped **at expansion** — before
the episode runs — and written into the trace itself, inside `config`. An
experiment, cell, or release identifier not written at expansion is gone
forever, and a trace read years later without a control plane still has to say
what produced it.

```python
config["provenance"] = {
    "schema_version": 1,
    "experiment_id": ...,      # None until a design object exists
    "experiment_name": ..., "design_sha256": ...,
    "release_id": ..., "release_version": ..., "declaration_sha256": ...,
    "cell_id": ..., "episode_id": ..., "episode_idx": ..., "attempt": ...,
    "seed": ..., "item_id": ..., "item_bank_sha256": ..., "oracle_version": ...,
    "participants": [{"participant_id", "kind", "binding", "config_sha256"}],
    "item_attributes": {},     # per-episode draws of randomized item attributes
}
```

The database's dimension and fact tables are a **projection** of that block,
not an independent source of truth, which is what makes the star schema
rebuildable: drop it and re-project. Promotion into columns
(`experiment_id`, `release_id`, `item_id`, `attempt`, `seed`, `status`) is
therefore a query-performance decision that can be revised at any time.

Seeds are derived, never drawn: `derive_seed(root_seed, cell_id, episode_idx)`
is a pure function, because `--shard-index` and `--resume` change how many
episodes a process enumerates and a generator would hand the same episode
different seeds depending on how it was run.

Events are appended to `<results>/<experiment>/<episode_uid>.events.jsonl` and
flushed per event, so an episode interrupted between two events is recoverable
as a partial trace with no Redis configured. A recovered trace is stored with
`status = PARTIAL`: evidence for inspection and retry, never a result.

---

## 7. Judge contract

The judge reads `EpisodeDataset` records — not Firestore documents, not a
environment-specific shape.

**Shared** (`a2a-judge/`): transcript prompt construction, judgment storage and
resume.

**Environment-specific** (`games/<environment>/`): rubrics, taxonomies, golden sets, prompt
versions. See `games/negotiation/negotiation_judge/`.

Judgments are addressed by a compound key:

```
{episode_uid}__{judge_model}__{prompt_version}
```

Resume is keyed on `(episode_uid, prompt_version)`, never `episode_uid` alone. Bumping
the prompt version is how you force a re-judge; conflating them would silently
mix judgments from two different rubrics into one aggregate.

---

## 8. LLM call robustness

One policy, in `a2a_engine.llm.retry`, shared by every environment.

```python
RetryPolicy(
    max_attempts=10,
    backoff_base=2.0, backoff_max=120.0,   # capped exponential
    jitter_ratio=0.25,                      # anti-thundering-herd
    request_cooldown=1.0,                   # prevention, not just recovery
    on_exhausted="return_none",             # or "raise"
)
```

Rules:

- **Status code beats string matching.** A 400 whose body mentions "timeout" is
  a malformed request; retrying it burns quota. Message matching is only the
  fallback for providers that raise statusless exceptions.
- **`Retry-After` wins over local backoff**, capped by `backoff_max`. Both paths
  are jittered — without it, parallel agents that hit one 429 retry in lockstep
  and trip the limit again.
- **Exhaustion behavior is an explicit choice.** `raise` propagates
  `RetryExhausted`; `return_none` degrades so the caller can fall back. It is a
  policy field rather than an accident of which helper you called.
- **Async callers use `acall_with_retry`.** It sleeps with `asyncio.sleep`, so a
  backing-off agent yields the loop instead of stalling every other agent.
- **Non-retryable errors propagate unchanged** — a bad API key surfaces as
  itself, not wrapped.

`compute_delay` is pure, so the backoff schedule is tested directly rather than
by timing sleeps.

---

## Where the seams are tested

Consolidation moved real behavior between layers. Each seam has tests pinning
what must not regress:

| Seam | Tests |
|---|---|
| Storage abstraction (was inline S3 in the runner) | `a2a-engine/tests/test_storage.py` |
| Preset inheritance + resolve hook | `a2a-engine/tests/test_experiment.py` |
| Manifest / reproducibility | `a2a-engine/tests/test_manifest.py` |
| Registry declarations | `a2a-engine/tests/test_registry.py` |
| Runner wiring end to end | `expt-runner/tests/test_runner_seam.py` |
| Negotiation HTTP → in-process conversion | `games/negotiation/tests/test_plugin_seam.py` |
| Judgment storage + resume | `a2a-judge/tests/test_store.py` |
| LLM retry/backoff/cooldown consolidation | `a2a-engine/tests/test_llm_retry.py` |
| Negotiation agents on the shared policy | `games/negotiation/tests/test_agent_retry_seam.py` |

```bash
uv run pytest a2a-engine/tests expt-runner/tests a2a-judge/tests games/negotiation/tests
```
