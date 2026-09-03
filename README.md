# a2a-comm

A framework for multi-agent communication experiments: one engine, one
experiment runner, one analysis layer — and a environment per release.

```
a2a-comm/
  a2a-engine/     contract + runtime: schemas, agents, LLM clients, registry,
                  dataset, tracing, ratings, EpisodeManifest, EpisodeStore backends
  expt-runner/    CLI: expand -> run -> persist
  a2a-judge/      LLM-as-judge scaffolding: prompts, judgment store, resume
  a2a-viewer/     browser control plane, trace viewer, and per-environment replay apps
  local_stack/    local control-plane service: releases, experiments, launches
  site/           static landing page (GitHub Pages) linking out to environment sites
  experiments/    cross-environment experiment configs + shared sink definitions
  scripts/        new_game.py scaffold
  games/
    buyer-seller/ sequential bargaining under asymmetric information
    calendar/     multi-agent meeting scheduling
    negotiation/  resource negotiation with cheap talk
    word-guess/   minimal reference environment
  docs/
```

## Quickstart

```bash
uv venv
uv pip install -e a2a-engine -e expt-runner -e a2a-judge
uv pip install -e games/buyer-seller -e games/word-guess \
               -e games/calendar     -e games/negotiation

cp .env.example .env      # add keys for the providers you use
```

Verify everything, without API keys or model spend — all four games run with
scripted agents and write to a local SQLite file:

```bash
a2a-run experiments/all_games_smoke.yaml --smoke-test
```

## Local stack and browser control plane

One command brings up the whole thing on Docker Desktop — runner, Redis, SQLite
trace store, and the web control plane. No cloud account, no API key:

```bash
docker compose up --build
```

| | |
|---|---|
| <http://localhost:8080/control.html> | launch experiments, watch launches, open episodes and replays |
| <http://localhost:8080/> | browse the trace corpus and the Calendar leaderboard |

From the control plane you pick an installed environment release and a checked-in
experiment YAML, launch a launch, and follow it live. Each episode links to its
persisted trace and to a per-environment replay of its Redis event stream. The browser
never uploads code, a Dockerfile, or an image — it selects releases and configs
that are already in the workspace.

Two environments are set up as worked examples:

```text
Calendar     games/calendar/experiments/typed_local_smoke.yaml
Negotiation  games/negotiation/experiments/smoke_local.yaml
```

The default job uses scripted agents, so it has no cloud or API dependency. See
`docs/LOCAL_STACK.md` for episode statuses, follow-up jobs, local model
endpoints, and the single-host SQLite boundary; `docs/RUNTIMES_AND_REPLAY.md`
covers the per-environment runtime images and replay apps.

```
[1/3] Sink reachability
      OK   sqlite [./results/a2a_traces.db] (4ms): write/read/delete round-trip succeeded
[2/3] Experiment expansion
      OK   word_guess -> environment=word_guess
      OK   buyer_seller -> environment=buyer_seller
      OK   calendar -> environment=calendar
      OK   negotiation -> environment=negotiation
[3/3] End-to-end write/read via sqlite (4 runs, scripted agents)
      ...
PASS  4/4 runs persisted and read back  | sink: sqlite
```

Then run something real:

```bash
a2a-run games/buyer-seller/experiments/example.yaml --dry-run   # check model access
a2a-run games/buyer-seller/experiments/example.yaml             # go
```

## The workflow

**Keys in `.env`, everything else as config.** Experiment YAML references
`${VAR}`; `.env` supplies the value. Nothing account-specific gets committed.

**Where data goes is a config block.** Four sinks — `sqlite` (default, zero
setup), `local` JSON, `s3`, `firestore` — selected per experiment and inheritable
from shared named definitions:

```yaml
storage:
  extends: local_db        # defined in experiments/storage.yaml
  path: ./results/pilot.db
```

See `docs/STORAGE.md`.

**Three execution modes**, answering different questions:

| | question | needs keys | writes |
|---|---|---|---|
| `--smoke-test` | is my data pipeline wired end to end? | no | yes, then reads back |
| `--dry-run` | are my models reachable? | yes | no |
| *(neither)* | the experiment | yes | yes |

**Analysis is sink-agnostic** — switching storage does not change analysis code:

```python
from a2a_engine import EpisodeDataset
ds = EpisodeDataset.from_config({"backend": "sqlite", "path": "./results/a2a.db"})
ds.to_episodes_df(); ds.to_messages_df(); ds.to_events_df()
```

## Adding a environment

```bash
python scripts/new_game.py my-environment
uv pip install -e games/my-environment
a2a-run games/my-environment/experiments/example.yaml --smoke-test
```

The scaffold passes its smoke test before you write any environment logic. See
`docs/ADDING_A_GAME.md` and `CONTRIBUTING.md`; `games/buyer-seller/` is the
reference implementation.

Games are found through an `a2a_engine.environments` entry point, so `a2a-run` works on
any installed environment without importing it first.

## Tests

```bash
uv run pytest                          # the whole offline suite
python scripts/release_check.py        # release-hazard scan, no network
```

None need API keys, a server, or cloud credentials. CI runs the same suite on
3.11 and 3.12, plus the cross-environment smoke test, every per-environment runtime release,
package builds, and a full Compose bring-up that asserts the trace, OTel,
artifact, leaderboard, and replay APIs.

## Docs

| | |
|---|---|
| `docs/CONTRACTS.md` | the environment, trace, storage and judge interfaces |
| `docs/ADDING_A_GAME.md` | adding an release |
| `docs/STORAGE.md` | sinks, config inheritance, the data loader |
| `docs/LOCAL_STACK.md` | cloud-free Docker Compose collaboration stack |
| `docs/AGENT_CONFIGURATION.md` | agent YAML, the agent pool, API formats and credentials |
| `docs/DATA_EXPLORATION.md` | SQLite analysis, viewer API and leaderboards |
| `docs/RUNTIMES_AND_REPLAY.md` | per-environment runtime images, Redis streams, replay and recovery |
| `docs/VIEWER_EXTENSIONS.md` | viewer and control-plane front-end structure |
| `CONTRIBUTING.md` | setup, house style, PR checklist |

## Status and known limitations

What this repo commits to is the environment contract, the runner, and the
trace/analysis layer. Everything below is known, deliberate, and open.

**The judge is not yet cross-environment.** `games/negotiation/negotiation_judge/`
still imports negotiation's own dataset layer rather than `EpisodeDataset`. Its
storage and resume lifecycle are already environment-agnostic; the rubric and taxonomy
modules are not. Researchers are expected to bring their own judge for now.

**LLM transport is duplicated.** The robustness layer — retry classification,
backoff, `Retry-After`, jitter, cooldown — lives once in `a2a_engine.llm.retry`.
The transport under it does not: `a2a_engine/llm/api.py` and
`games/negotiation/negotiation_game/backend/agents/api.py` are near-identical
SSE parsers and payload builders. The engine's copy is the more advanced one
(it carries OTel spans), so the merge direction is negotiation's onto the
engine's, lifting `call_llm_oneshot_with_thinking` across.

**`--smoke-test` runs scripted agents by design.** It proves the environment and data
paths without model spend. Live-provider runs are a separate, explicit mode.

**Negotiation's scenario pools are configs, not results.** Agent episodes are
never committed; they are experiment output and belong in a configured sink.
