# Adding a environment

A environment is a Python class that produces a `EpisodeTrace`. Everything else —
LLM clients with retries, parallel fan-out, YAML experiments, OTel tracing,
storage backends, manifests, the dataset layer — is supplied.

The worked reference throughout is `games/buyer-seller/`, the smallest environment that
exercises the full contract. `games/word-guess/` is smaller still if you want the
absolute minimum.

## 1. Scaffold it

```bash
python scripts/new_game.py my-environment
```

That writes a working package — config, environment loop, scripted agents, experiment
YAML, `run.py`, tests — that passes its smoke test immediately. Install and
check:

```bash
uv pip install -e games/my-environment
a2a-run games/my-environment/experiments/example.yaml --smoke-test
```

Then replace the placeholder logic. The scaffold now creates a typed
`environments/<environment>_tiny_v1.yaml` plus an episode-driven experiment. Add task
inputs there with local relative paths and SHA-256s before claiming a portable
release. The rest of this document explains what it generated and why.

For a new release, prefer the typed `ReleaseDeclaration` +
`ExperimentConfig` pair described in [CONTRACTS.md](CONTRACTS.md#typed-release-and-experiment-yaml-v1).
The shipped Calendar reference is
`games/calendar/environments/calendar_tiny_v1.yaml` plus
`games/calendar/experiments/typed_local_smoke.yaml`; it pins its task corpus,
runs with no cloud service, writes SQLite, and retains full local OTel capture.
For an editor/CI-friendly copy of the formal contract, generate its JSON Schema
with `uv run python scripts/export_config_schemas.py --output-dir schemas`.

## 2. Anatomy

```
games/buyer-seller/
  SPEC.md                    the protocol, in prose — write this first
  pyproject.toml             package + the a2a_engine.environments entry point
  run.py                     thin wrapper: load .env, import, defer to the CLI
  buyer_seller/
    __init__.py              imports environment.py, triggering register_environment
    environment.py                  config schema + the loop
    agents.py                LLM agents: prompts in, actions out
  environments/
    buyer_seller_tiny_v1.yaml  portable world + pinned inputs + metric contract
  experiments/
    example.yaml             agents + episode plan + local storage + OTel
  tests/
    test_buyer_seller.py     protocol tests, scripted agents, no API keys
```

## 3. The five pieces

### 3.1 Config schema

Subclass `EpisodeConfigBase`; add only what is domain-specific. Generic fields
(`agents`, `seed`, `num_agents`, `episode_id`) are inherited.

```python
from pydantic import Field
from a2a_engine import EpisodeConfigBase

class BuyerSellerConfig(EpisodeConfigBase):
    environment_id: str = "buyer_seller"
    num_agents: int = 2
    num_items: int = Field(default=3, ge=1)
    seller_cost: float = Field(default=10.0, ge=0)
    discount_factor: float = Field(default=0.9, gt=0, le=1.0)
```

**The rule that makes runs reproducible:** anything that changes model behavior
must be a config field, because the resolved config is what lands in the trace.
A knob read from an env var or hardcoded in the environment is a knob nobody can
recover from the record later.

### 3.2 Environment class

Two methods: `__init__(config, dry_run)` and `run() -> EpisodeTrace`. Use
`EventLog` rather than building `Event`s by hand — it timestamps for you.

```python
import asyncio, uuid
from a2a_engine import EventLog, EpisodeTrace, register_environment

class BuyerSellerGame:
    def __init__(self, config: dict | BuyerSellerConfig, dry_run: bool = False):
        self.config = config if isinstance(config, BuyerSellerConfig) \
                      else BuyerSellerConfig(**config)
        self.dry_run = dry_run
        self.events = EventLog()

    def run(self) -> EpisodeTrace:
        return asyncio.run(self._run_async())   # sync entry point; async inside

    @classmethod
    def build_program(cls):
        from .workflow import build_program
        return build_program()

    async def _run_async(self) -> EpisodeTrace:
        from .workflow import GameContext, recursion_limit
        result = await self.build_program().ainvoke(
            {}, context=GameContext(self),
            config={"recursion_limit": recursion_limit(self.config)},
        )
        return result["trace"]
```

`run()` remains synchronous and wraps graph execution with `asyncio.run`. The game owns event emission and `EpisodeTrace` assembly in its finalization node; raw graph state is not the trace contract. The example's context and limit are game-owned helpers; use the shipped workflow modules as executable references.

### Executable workflow and remote adapters

All four shipped games execute local LangGraph programs. Author a `StateGraph` in `workflow.py`, wrap it with `GraphProgram`, and expose it through side-effect-free `build_program()`. The same builder supplies execution and `GET /api/environments/<id>/workflow`; do not write a descriptive provider, parallel edge table, renderer-specific graph catalog, or event-to-node mapping.

- Put counters, candidates and explicit scheduling outcomes in graph state; keep agents, private domain objects, event sinks, sockets and waiters in invocation context.
- Declare every agent handoff with `AgentStep(actor_selector, operation, observation_builder, adapter)`. Domain leaves may calculate, validate and commit effects but must not hide another invocation, round loop or retry loop. The invocation gateway catches exercised violations, not arbitrary Python bypasses.
- Use finite executable route maps for optional phases, termination and validation retries. Size the superstep limit from game bounds, not LangGraph's default. Real subgraphs provide navigation; `BatchStep` exposes actual bounded fan-out/join execution, including per-seat serialization where required.
- Expose a class-level `runtime_capability()` for execution-only environment tools and context binding. Add a game-owned staged submission tool in `remote.py` and agent-owned strategy in `runtime_strategy.py`. Provider execution, prompts/parsers and history stay agent-side; authorization, validation, resolution and events stay environment-side.
- New separate-container examples use `a2a-turns/2`; `/1` is an explicit legacy loopback adapter. Acknowledgement is not completion or permission to commit. Missing/failed results follow the game's established policy, never an invented generic fallback.

Export reads executable bindings, routes and actual subgraphs without constructing an episode, evaluating selectors/guards, resolving tasks, running a solver, making provider calls or contacting a runtime. It shows possible control flow, not a historical replay or communication-permission graph. Source references are package-relative symbols, not source-reading endpoints; the structural digest is not a hash of every domain algorithm.

Test exact observations/prompts, normalized events, final state and metrics against a frozen test-only baseline, including untaken routes and failure/timeout/cleanup paths. Add fail-fast export doubles, ordinary installed-wheel checks, real isolated container execution and generic API/browser coverage. Never keep the frozen orchestration as a second production driver. See `docs/AGENT_RUNTIME_CONTRACT.md` and `docs/LOCAL_STACK.md` for the supported provisioning deployment.

### 3.3 Agents

Subclass `LLMAgent` and override `build_messages` / `parse_response`. Name them
so the OTel spans read well.

```python
class SellerAgent(LLMAgent):
    def __init__(self, client, *, cost: float):
        super().__init__(client, system_prompt=SELLER_SYSTEM.format(cost=cost),
                         name="seller")

    def build_messages(self, observation, tools):
        return [{"role": "system", "content": self.system_prompt},
                {"role": "user", "content": self.build_prompt(observation)}]

    def parse_response(self, text, observation, tools):
        match = _PRICE_RE.search(text or "")
        if match is None:
            return {"price": observation["your_last_offer"], "parse_failed": True}
        return {"price": float(match.group(1)), "parse_failed": False}
```

Two things worth copying from `buyer_seller/agents.py`:

**Parse forgivingly, then flag.** A trajectory costs real money. Falling back to
a defined default and setting `parse_failed` lets analysis filter those rounds;
raising throws away the whole run over a formatting slip.

**Enforce information boundaries in the observation, not the prompt.** If your
environment has private information, build each side's observation explicitly and never
put the other side's secret in it. A prompt instruction is a request; an absent
dict key is a guarantee.

### 3.4 Scripted agents for `--dry-run` and `--smoke-test`

Branch on `self.dry_run` and substitute stand-ins that need no LLM:

```python
def _build_agents(self):
    if self.dry_run:
        self.seller = _ScriptedSeller(...)
        self.buyer = _ScriptedBuyer(...)
        return
    self.seller = SellerAgent(make_llm_client(seller_cfg), ...)
```

Make them **deterministic** given the config. That way a smoke test produces the
same trajectory every time, and a diff in the trace means a real behavior change
rather than noise. It is also what lets your tests assert on exact prices.

### 3.5 Register

```python
register_environment(
    "buyer_seller",
    BuyerSellerGame,
    package="buyer-seller",     # for game_package_version in the manifest
    dry_run_checks_keys=False,  # scripted agents need no keys
    # storage={"backend": "sqlite", "path": "./results/bs.db"},   # optional default
    # resolve_config=resolve_config,                              # optional, see §5
    # rating_adapter=MyRatingAdapter(),                           # optional, see §5.1
)
```

Then declare the entry point so `a2a-run` finds the environment without anyone
importing it:

```toml
[project.entry-points."a2a_engine.environments"]
buyer_seller = "buyer_seller"
```

Set `dry_run_checks_keys=False` when your dry run substitutes non-LLM agents.
Leave it `True` if your dry run still calls models with a shortened config —
then `--dry-run` asserts every agent has a usable key.

## 4. Event shape

Events whose `data` carries `{speaker, text}` are treated as messages: they
appear in `to_messages_df()` and in judge transcripts, for free, regardless of
the event's `type`.

```python
self.events.append("offer", data={
    "speaker": "seller",                       # -> transcript
    "text": f"I offer one unit at {price:.2f}.",
    "price": price, "round": t,                # -> structured analysis
})
```

Carrying both means one event serves the transcript and the metrics. Everything
else lands in `to_events_df()` and the viewer's default JSON renderer.

## 5. Optional: `resolve_config`

If your environment derives config from config — sampling a task, running a solver to
generate a scenario — do it in a `resolve_config(config) -> config` hook rather
than inside the environment. The runner calls it **once per cell, before fan-out**, and
merges the output into every run's config, so it lands in the trace.

```python
def resolve_config(config: dict) -> dict:
    resolved = dict(config)
    if resolved.get("difficulty"):
        resolved["scenario"] = generate_scenario(resolved["difficulty"])
    return resolved
```

This is what makes a stochastically generated scenario reproducible: the
generated artifact is recorded, not just the seed that produced it.

### 5.1 Optional: trace-derived ratings

If a environment belongs on the shared leaderboard, declare an adapter at registration
time. The adapter runs only after a trace is complete; never emit a rating from
inside the environment loop. This separation makes backfills repeatable and prevents a
live model session from skewing reporting.

```python
from a2a_engine.ratings import MetricSpec, RatingEvent

class MyRatingAdapter:
    environment_id = "buyer_seller"
    version = "buyer-seller-rating-v1"
    metrics = [MetricSpec(name="utility", higher_is_better=True)]

    def extract(self, trace: dict, artifacts: dict) -> RatingEvent | None:
        # Read only the completed trace and digest-matched artifacts.
        ...

register_environment("buyer_seller", BuyerSellerGame, rating_adapter=MyRatingAdapter())
```

Put every input needed for future extraction in the final trace. If a score
needs an expensive offline analysis, write a versioned `DerivedArtifact` tied to
the trace digest instead of modifying the source trace. The engine replays all
completed episodes to create SQLite rating events and snapshots.

## 6. Release and experiments

The primary authoring path is a pair of strict Pydantic-validated YAML files.
The release owns the world, roles, input hashes, metric declarations, and
adapter intent. The experiment owns agent configuration, a concrete episode
plan, local storage, and observability. Together they contain everything a
collaborator needs to start the same environment; the resolved config, release
hash, source version, and input digests are retained in the completed trace.

```yaml
# environments/buyer_seller_tiny_v1.yaml
schema_version: 1
id: buyer-seller.tiny
release: v1
engine:
  environment_id: buyer_seller
  defaults: {num_agents: 2, num_items: 3}
roles: [{id: buyer, count: 1}, {id: seller, count: 1}]
metrics: [{name: utility, producer: environment, direction: maximize}]
adapter_bindings: {model: engine.llm, communication: local.in_process}
```

```yaml
# experiments/example.yaml
schema_version: 1
name: buyer_seller_example
release: ../environments/buyer_seller_tiny_v1.yaml
agents:
  - {role: buyer, type: llm, model: gpt-4o-mini}
  - {role: seller, type: llm, model: gpt-4o-mini}
episodes:
  - {label: wide_surplus, count: 3, seeds: [1, 2, 3]}
storage:
  backend: sqlite
  path: ./results/buyer_seller.db
observability: {capture_content: true}
```

The runner expands each episode into the established cell runner, records a
typed release/episode reference in each trace, and writes local OTel JSONL
beside the results. See `docs/CONTRACTS.md` for every validated field and
`docs/STORAGE.md` for named SQLite sinks.

The older `defaults` + `cells` YAML remains supported for existing games,
but new games should start with `ReleaseDeclaration` + `ExperimentConfig`.

Run it three ways:

```bash
a2a-run experiments/example.yaml --smoke-test   # is my data pipeline wired?
a2a-run experiments/example.yaml --dry-run      # are my models reachable?
a2a-run experiments/example.yaml                # the real thing
```

## 7. Tests

Test the protocol, not the models. With deterministic scripted agents you can
assert exact outcomes and run in CI with no keys. From
`games/buyer-seller/tests/`:

- **Rule invariants** — the buyer never accepts above its value; offers never
  rise when monotonicity is on.
- **Arithmetic** — utilities match the discounted-surplus formula stated in
  `SPEC.md`.
- **Terminal conditions** — stops at inventory exhaustion, never exceeds the
  deadline, no-trade yields zero.
- **Information boundaries** — the private value is not present anywhere in the
  opponent's observation dict.
- **Determinism** — same config in, identical trajectory out.
- **Trace shape** — `to_messages_df()` is non-empty and speakers are what you
  expect.

## 8. Checklist

- [ ] `SPEC.md` states the protocol, including any choices the source is silent on
- [ ] `register_environment(...)` runs on package import
- [ ] `a2a_engine.environments` entry point declared in `pyproject.toml`
- [ ] `dry_run=True` runs with no API keys and is deterministic
- [ ] Message events carry `{speaker, text}`
- [ ] `final_state` and `metrics` are populated
- [ ] `environments/<environment>_tiny_v1.yaml` validates, describes roles/resources,
  and pins every task input with a SHA-256
- [ ] `experiments/example.yaml` selects that release, agents, episodes,
  SQLite storage, and `observability.capture_content: true`
- [ ] `a2a-run experiments/example.yaml --smoke-test` passes
- [ ] Protocol tests pass with no keys
- [ ] Execution and inspection use one side-effect-free `build_program()` with explicit handoffs, finite routes and bounded recursion
- [ ] Remote adapters/strategies pass `/2` parity and isolated Compose smoke; installed wheels export without execution or credentials
- [ ] Every declared derived metric has a registered deterministic extractor;
  `scripts/materialize_metrics.py --database ... --environment <environment>` is idempotent
- [ ] Package-owned `tests/smoke.yaml` declared as `tool.groundwork.smoke_config`
- [ ] Agent-side `a2a_agent.seats` entry point declared when the game supports admitted seats
- [ ] Browser fixtures and chat assertions live in `games/<name>/tests/browser/`
- [ ] Display metadata lives in the game's `catalogue.json`; shared lists are generated

## 9. Where things live

| You want to … | Look at |
|---|---|
| Author executable orchestration | `a2a_engine.workflow_runtime`, `games/buyer-seller/buyer_seller/workflow.py` |
| Assemble the trace and events | `a2a_engine.schemas.EpisodeTrace`, `a2a_engine.tracing.EventLog` |
| Subclass an agent | `a2a_engine.agent.LLMAgent` |
| Build an LLM client from config | `a2a_engine.llm.factory.make_llm_client` |
| Choose where episodes go | `docs/STORAGE.md` |
| Analyze episodes | `a2a_engine.dataset.EpisodeDataset` |
| Declare a portable world and episode plan | `a2a_engine.environment.ReleaseDeclaration`, `ExperimentConfig` |
| Add a trace-derived metric | `a2a_engine.derived_metrics.DerivedMetricExtractor` |
| Add an event renderer to the local viewer | `docs/VIEWER_EXTENSIONS.md` |
| Understand the contracts | `docs/CONTRACTS.md` |
| Copy a small complete environment | `games/buyer-seller/` |

## Package-owned integration

A new game does not edit the agent CLI, strategy factory, CI package lists,
root smoke inventory, frontend route list, or landing-page catalogue. Declare
these integration points in its own `pyproject.toml`:

```toml
[project.entry-points."a2a_agent.seats"]
my_game = "my_game.seat_plugin:seat_plugin"

[tool.groundwork]
smoke_config = "tests/smoke.yaml"
browser_fixtures = "tests/browser/fixtures.yaml"
site_metadata = "catalogue.json"
```

Export a `SeatPlugin` from `a2a_agent.seat_plugins`, with a factory accepting
`PolicyContext`. Ordinary policies use `context.model_client()`; specialized
backends can consume the validated `context.config`. Advertise supported policy
kinds and the runtime adapter (`a2a-turns/2`, or the legacy `/1` adapter). Declare
`a2a-agent` as a dependency when importing that interface. Models and history
stay agent-side; the environment retains observations and commit authority.

`tests/smoke.yaml` contains one small engine configuration, including the
`environment_id` and any required capabilities. The workspace CLI collects
these into the all-environment experiment; it does not contact providers.
`tests/browser/fixtures.yaml` contains a list of `{id, config}` rows with explicit
scripted/heuristic agents. Rows may use different task versions of the same
environment. Browser assertions are ordinary `*.spec.ts` files in the same
directory, automatically collected by Playwright. Import `expect` and `test` from
`../../../../web/tests/support/playwright` so the specs use the shared web dependencies. Inputs and committed outputs
use the common trace contracts and chat renderer.

```bash
python scripts/install_workspace.py
python -m pip install -r requirements-dev.txt
python scripts/export_environment_ids.py  # before Next dev/build
python scripts/workspace.py export-site
python scripts/workspace.py test -q
python scripts/workspace.py smoke
python scripts/workspace.py build
python scripts/workspace.py check-wheels
ruff check .
ruff format --check .
```

The installed-wheel check compares against a separately generated source
manifest, so skipping a game during build/install fails verification. Discovery
never runs fixtures, constructs environments, or makes model calls. Generated
frontend routes and site catalogues are build outputs and are not committed.
Ruff is pinned in `requirements-dev.txt` and enforced in CI for discovery,
agent dispatch, and game seat-plugin files; older scientific modules are outside
this initial formatting scope. A new transport or trace contract remains a
shared infrastructure change and should land before its dependent game PRs.
