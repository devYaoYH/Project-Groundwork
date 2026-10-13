# Project Groundwork

A framework for multi-agent communication experiments: one engine, one
experiment runner, one analysis layer, and an environment per release.

Start locally with the researcher workspace, or ask a coding agent to set it up
and help author a game. [Installation and copyable agent prompts](docs/INSTALLATION.md)
cover both paths; [AGENTS.md](AGENTS.md) describes the repository's invariants.

```
Project-Groundwork/
  a2a-engine/     contract + runtime: schemas, agents, LLM clients, registry,
                  dataset, tracing, ratings, EpisodeManifest, EpisodeStore backends
  a2a-agent/      agent-side runtime, strategies, model harnesses and history
  expt-runner/    CLI: expand -> run -> persist
  a2a-judge/      LLM-as-judge scaffolding: prompts, judgment store, resume
  web/            Next.js researcher control plane, static-exported and served
                  by cloud_platform or local_stack
  local_stack/    local control-plane service: releases, experiments, launches
  cloud_platform/ authenticated control API, owner-scoped evidence, infrastructure
                  worker and bridge to separately pinned science runtimes
  skills/         installable agent workflows
  site/           static landing page (GitHub Pages) linking out to environment sites
  experiments/    cross-environment experiment configs + shared sink definitions
  scripts/        new_game.py scaffold
  games/
    <environment>/ packaged rules, seat plugins, item banks, tests and fixtures
  docs/
```

The installed environment catalogue is available at `/environments/`. Each game owns
its `SPEC.md` and integration metadata in `games/<environment>/pyproject.toml`.

## Quickstart

Install Git and start Docker Desktop (or Docker Engine with Compose on Linux).
No cloud account, model key, or host Python/Node installation is needed.

```bash
git clone https://github.com/devYaoYH/Project-Groundwork.git
cd Project-Groundwork
docker compose -f compose.platform.yml up --build --detach --wait
docker compose -f compose.platform.yml ps
curl --fail http://127.0.0.1:8080/api/health
```

Open [the local workspace](http://127.0.0.1:8080/experiments/) and choose
**Continue as Alice**. Pick an environment, create and lock an experiment, then
launch it in **smoke** mode. Inspect streamed logs, recorded episodes, and the
downloadable experiment export. Bob has a separate local workspace.

This stack has separate API, worker, and runtime containers. Verify execution,
private ownership, and exported evidence through the public HTTP contracts:

```bash
docker compose -f compose.platform.yml exec -T api \
  python deploy/verify_local.py --url http://127.0.0.1:8080
```

For an existing checkout, skip cloning and preserve its configuration and data.
[INSTALLATION.md](docs/INSTALLATION.md) covers port conflicts, parallel checkouts,
refreshes, Python 3.12 CLI setup, Node 22 frontend development, and copyable
prompts for any coding agent. The original unauthenticated
`docker compose` runner/viewer stack remains a distinct option described in
[LOCAL_STACK.md](docs/LOCAL_STACK.md). Hosted Google login and release deployment
are documented separately in [deploy/README.md](deploy/README.md).

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
| `--dry-run` | are my models reachable? | provider-dependent | no |
| *(neither)* | the experiment | provider-dependent | yes |

Scripted agents remain credential-free in live mode. A model-access dry run can
contact providers, so use smoke mode for setup without provider calls.

**Analysis is sink-agnostic** — switching storage does not change analysis code:

```python
from a2a_engine import EpisodeDataset
ds = EpisodeDataset.from_config({"backend": "sqlite", "path": "./results/a2a.db"})
ds.to_episodes_df(); ds.to_messages_df(); ds.to_events_df()
```

## Adding an environment

With the [CLI development environment](docs/INSTALLATION.md#python-development-and-the-cli)
activated:

```bash
python scripts/new_game.py my-environment
python -m pip install -e games/my-environment
python -m pytest games/my-environment/tests
a2a-run games/my-environment/experiments/example.yaml --smoke-test
```

The scaffold verifies the minimum CLI contract before you replace its rules.
Finish the declaration, executable workflow, and adapters your game requires.
See [ADDING_A_GAME.md](docs/ADDING_A_GAME.md) and
[CONTRIBUTING.md](CONTRIBUTING.md); `games/buyer-seller/` is the reference
implementation.

Games are found through an `a2a_engine.environments` entry point, so `a2a-run` works on
any installed environment without importing it first.

## Tests

```bash
python -m pytest                       # the whole offline suite
python scripts/release_check.py        # release-hazard scan, no network
```

These checks need no model keys or cloud credentials; some exercise local HTTP
servers and loopback agents. CI uses Python 3.12 and Node 22, checks scripted
smoke/readback and runtime releases, builds packages and the UI, and verifies
Compose APIs. Install development dependencies first as documented in the
installation guide.

## Docs

| | |
|---|---|
| [docs/INSTALLATION.md](docs/INSTALLATION.md) | local setup, development prerequisites, and agent prompts |
| [AGENTS.md](AGENTS.md) | codebase, scientific invariants, and verification guidance |
| [skills/groundwork-development/SKILL.md](skills/groundwork-development/SKILL.md) | canonical installable setup/game-authoring workflow |
| `docs/CONTRACTS.md` | the environment, trace, storage and judge interfaces |
| `docs/ADDING_A_GAME.md` | adding an release |
| `docs/STORAGE.md` | sinks, config inheritance, the data loader |
| `docs/LOCAL_STACK.md` | cloud-free Docker Compose collaboration stack |
| `docs/AGENT_CONFIGURATION.md` | agent YAML, the agent pool, API formats and credentials |
| `docs/DATA_EXPLORATION.md` | SQLite analysis, viewer API and leaderboards |
| `docs/RUNTIMES_AND_REPLAY.md` | per-environment runtime images, Redis streams, replay and recovery |
| `docs/VIEWER_EXTENSIONS.md` | viewer and control-plane front-end structure |
| `CONTRIBUTING.md` | setup, house style, PR checklist |
| [deploy/README.md](deploy/README.md) | hosted platform, immutable runtime bindings, and release CI |

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
