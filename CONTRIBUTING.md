# Contributing

The main thing this repo wants from contributors is **new games**. The engine,
runner, storage and analysis layers exist so that adding an environment is a
day's work rather than a project.

## Setup

```bash
git clone <repo> && cd a2a-comm
uv venv
uv pip install -e a2a-engine -e expt-runner -e a2a-judge
uv pip install -e games/buyer-seller -e games/word-guess \
               -e games/calendar     -e games/negotiation

cp .env.example .env      # add keys for the providers you use
```

Verify the install without spending anything:

```bash
a2a-run experiments/all_games_smoke.yaml --smoke-test
pytest a2a-engine/tests expt-runner/tests games/buyer-seller/tests
```

The smoke test runs every game with scripted agents and writes to a local SQLite
file. If it passes, your checkout works.

## The four things a researcher does

**A) Keys in `.env`.** Never in YAML. Experiment files reference `${VAR}`;
`.env` supplies the value. See `.env.example`.

**B) Endpoints and models as config.** Where traces go is a `storage:` block;
which models play is an `agents:` list. Both live in the experiment file, both
get recorded in the trace. See `docs/STORAGE.md`.

**C) Run through `a2a-run`.** One CLI, all games, three modes:
`--smoke-test` (data path), `--dry-run` (model access), neither (real).
`--resume`, `--shard-index/--shard-count` and `--max-parallelism` handle scale.

**D) Analyse through `GameDataset`.** Sink-agnostic, so switching storage does
not change analysis code:

```python
from a2a_engine import GameDataset
ds = GameDataset.from_config({"backend": "sqlite", "path": "./results/a2a.db"})
ds.to_games_df(); ds.to_messages_df(); ds.to_events_df()
```

## Adding a game

```bash
python scripts/new_game.py my-game
uv pip install -e games/my-game
a2a-run games/my-game/experiments/example.yaml --smoke-test
```

The scaffold passes its smoke test before you write any game logic, so the first
thing you change is the rules. Read `docs/ADDING_A_GAME.md` for what it
generated and why, and copy `games/buyer-seller/` as the reference.

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
an environment variable at runtime is a knob nobody can recover afterwards.

**Test protocols, not models.** Deterministic scripted agents let you assert
exact outcomes and run in CI for free. Tests that call real models test the
provider, not your game.

**Enforce information boundaries structurally.** If your game has private
information, build each agent's observation explicitly and leave the secret out.
A prompt instruction is a request; an absent dict key is a guarantee.

**Remote writes are best-effort, local writes are not.** A network failure must
never cost a batch of model spend. Record the failure in the manifest and carry
on — that invariant is why `S3TraceStore` returns the local path when an upload
fails.

## Adding a storage backend

Implement `put_trace` / `get_trace` / `list_traces` / `check` and call
`register_store`. See the end of `docs/STORAGE.md`; `a2a_engine/storage/sqlite.py`
is the shortest complete example.

## Judge

Cross-game judge usability is explicitly **P2**. The judge in
`games/negotiation/negotiation_judge/` is negotiation-specific today, and the
expectation is that researchers bring their own judging workflows. What this repo
commits to is the game contract, the experiment runner, and the trace/analysis
layer. If you want a judge, `GameDataset` gives you transcripts in a uniform
shape across every game — start there.
