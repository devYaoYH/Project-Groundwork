# Contributing

The main thing this repo wants from contributors is **new games**. The engine,
runner, storage and analysis layers exist so that adding an release is a
day's work rather than a project.

## Setup

For the browser workspace, start with
[docs/INSTALLATION.md](docs/INSTALLATION.md). For Python development, use a seeded
Python 3.12 environment and install all registered workspace packages:

```bash
git clone https://github.com/devYaoYH/Project-Groundwork.git
cd Project-Groundwork
uv venv --python 3.12 --seed
source .venv/bin/activate
python scripts/install_workspace.py
python -m pip install -r requirements-platform.txt
```

Verify the install without spending anything:

```bash
a2a-run experiments/all_games_smoke.yaml --smoke-test
python -m pytest a2a-engine/tests expt-runner/tests games/buyer-seller/tests
```

The smoke test runs every environment with scripted agents and writes to a local SQLite
file. If it passes, your CLI checkout works. The installer includes `a2a-agent`
and every registered game; the additional requirements install pytest and the
platform's development dependencies. Provider keys are needed only for requested
live-provider work; keep existing private configuration when setting them up.
Read [AGENTS.md](AGENTS.md) for scientific pins, shared lifecycle, and ownership
invariants.

## The four things a researcher does

**A) Keys in `.env`.** Never in YAML. Experiment files reference `${VAR}`;
`.env` supplies the value. See `.env.example`.

**B) Endpoints and models as config.** Where episodes go is a `storage:` block;
which models play is an `agents:` list. Both live in the experiment file, both
get recorded in the trace. See `docs/STORAGE.md`.

**C) Run through `a2a-run`.** One CLI, all games, three modes:
`--smoke-test` (data path), `--dry-run` (model access), neither (real).
`--resume`, `--shard-index/--shard-count` and `--max-parallelism` handle scale.

**D) Analyse through `EpisodeDataset`.** Sink-agnostic, so switching storage does
not change analysis code:

```python
from a2a_engine import EpisodeDataset
ds = EpisodeDataset.from_config({"backend": "sqlite", "path": "./results/a2a.db"})
ds.to_episodes_df(); ds.to_messages_df(); ds.to_events_df()
```

## Adding an environment

```bash
python scripts/new_game.py my-environment
python -m pip install -e games/my-environment
python -m pytest games/my-environment/tests
a2a-run games/my-environment/experiments/example.yaml --smoke-test
```

The scaffold passes its minimum CLI smoke test before you replace its rules.
Finish the declaration, executable workflow, runtime capabilities and UI support
required by your game. Read [docs/ADDING_A_GAME.md](docs/ADDING_A_GAME.md) for the
contracts, and use `games/buyer-seller/` as the reference.

Before opening a PR:

- [ ] `SPEC.md` states the protocol, and records any call you made where the
      protocol was ambiguous
- [ ] `dry_run=True` runs with no API keys and is deterministic
- [ ] Message events carry `{speaker, text}`
- [ ] `--smoke-test` passes
- [ ] Protocol tests pass with no keys
- [ ] Registered in `experiments/all_games_smoke.yaml` and `site/games.json`

## House style

**Config is the record.** Anything that changes model behavior must be a config
field, because the resolved config is what lands in the trace. A knob read from
an release variable at runtime is a knob nobody can recover afterwards.

**Test protocols, not models.** Deterministic scripted agents let you assert
exact outcomes and run in CI for free. Tests that call real models test the
provider, not your environment.

**Enforce information boundaries structurally.** If your environment has private
information, build each agent's observation explicitly and leave the secret out.
A prompt instruction is a request; an absent dict key is a guarantee.

**Follow the selected durability contract.** Standalone mirror backends such as
`S3EpisodeStore` retain a local trace and record remote upload failures. The
hosted platform's durable event bridge instead requires an infrastructure ACK
before continuing, and final evidence must be published before a launch succeeds.
Do not turn hosted fail-closed writes into an optional mirror.

## Adding a model endpoint

Follow [the endpoint onboarding checklist](docs/AGENT_CONFIGURATION.md#onboard-a-model-endpoint)
through explicit provider sources, account credential references, API parameter
rules, shared agent bindings, CLI/design resolution, and runtime credentials. Updating the model registry alone does not
publish a choice in the Agents screen. Rebuild the selected platform and runtime
images before verifying container availability.

## Adding a storage backend

Implement `put_episode` / `get_episode` / `list_episodes` / `check` and call
`register_store`. See the end of `docs/STORAGE.md`; `a2a_engine/storage/sqlite.py`
is the shortest complete example.

## Judge

Cross-environment judge usability is explicitly **P2**. The judge in
`games/negotiation/negotiation_judge/` is negotiation-specific today, and the
expectation is that researchers bring their own judging workflows. What this repo
commits to is the environment contract, the experiment runner, and the trace/analysis
layer. If you want a judge, `EpisodeDataset` gives you transcripts in a uniform
shape across every environment — start there.
