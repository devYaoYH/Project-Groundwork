# Install and develop Groundwork

Groundwork has a local researcher workspace and a Python experiment CLI. The
recommended workspace runs three containers: the control API/UI, the
infrastructure worker, and the science runtime. It supports local Alice/Bob
identities, private experiments, streamed logs, and downloadable results without
a cloud account or model API key.

## Local workspace

Install Git and Docker Desktop, or Docker Engine with the Compose plugin on
Linux. Start Docker before running these commands. Python and Node are built
into the container images; they are not required on the host for this path.
The shell examples also work in a POSIX shell such as WSL on Windows.

From a new checkout:

```bash
git clone https://github.com/devYaoYH/Project-Groundwork.git
cd Project-Groundwork
docker compose -f compose.platform.yml up --build --detach --wait
docker compose -f compose.platform.yml ps
curl --fail http://127.0.0.1:8080/api/health
```

For an existing checkout, start at the Compose command. The first build installs
Python packages and builds the web UI, so it takes longer than a restart.

Open [the local workspace](http://127.0.0.1:8080/experiments/) and choose
**Continue as Alice**. Select an installed environment, create and lock an
experiment, and choose **smoke** when launching. Open its launch to watch logs,
then inspect the completed episodes and download the joined experiment export.
Sign out and continue as Bob to see a separate experiment list. These identities
are for local development; hosted workspaces use Google sign-in.

Verify the whole path automatically using the already-built API container:

```bash
docker compose -f compose.platform.yml exec -T api \
  python deploy/verify_local.py --url http://127.0.0.1:8080
```

The checker creates a scripted experiment as Alice, waits for the separate
worker/runtime, checks persisted evidence and ZIP checksums, and verifies that
Bob and anonymous clients cannot read Alice's results. It does not start another
worker or call a model provider. A healthy API alone does not prove execution.

### Refresh and stop

The web export and Python packages are baked into images. After code changes,
rebuild/recreate the same stack and reload the browser:

```bash
docker compose -f compose.platform.yml up --build --detach --wait
docker compose -f compose.platform.yml logs --tail 80 worker runtime
docker compose -f compose.platform.yml down
```

`down` preserves the named data and execution volumes. Do not use `down -v` to
refresh code: it deletes those volumes and their local experiment data. Keep
private `.env` files and existing configuration when updating a checkout.

### Another checkout or a busy port

Use a different Compose project, free port, **and both image tags**. A distinct
platform tag alone can still overwrite the science runtime image used by another
checkout. For example, in a dedicated shell:

```bash
export PLATFORM_PORT=18080
export PLATFORM_IMAGE=groundwork-platform:my-checkout
export PLATFORM_RUNTIME_IMAGE=groundwork-runtime:my-checkout
docker compose -p groundwork-my-checkout -f compose.platform.yml up --build --detach --wait
docker compose -p groundwork-my-checkout -f compose.platform.yml ps
curl --fail http://127.0.0.1:18080/api/health
docker compose -p groundwork-my-checkout -f compose.platform.yml exec -T api \
  python deploy/verify_local.py --url http://127.0.0.1:8080
```

The checker runs inside the API container, so its internal port remains `8080`.
The browser uses the chosen host port. Reuse the project and environment values
for subsequent refreshes; stop this copy with
`docker compose -p groundwork-my-checkout -f compose.platform.yml down`.

### Original viewer stack

The original unauthenticated viewer remains available as a distinct workflow:

```bash
docker compose up --build --detach --wait viewer
docker compose run --rm runner
```

It uses `docker-compose.yml`, Redis, and a shared SQLite corpus. It does not
provide the platform's Alice/Bob ownership boundary. It also uses host port
`8080` by default, so stop the other stack or choose an explicit port override.
See [LOCAL_STACK.md](LOCAL_STACK.md) for this viewer, its CLI jobs, and the
separate isolated-agent deployment. These Compose files are alternatives;
do not combine them into one overlay.

## Python development and the CLI

Use Python **3.12 or later**. The following route uses `uv` to obtain Python 3.12
when it is absent from your PATH and seeds pip for the workspace installer:

```bash
uv venv --python 3.12 --seed
source .venv/bin/activate
python scripts/install_workspace.py
python -m pip install -r requirements-platform.txt
a2a-run experiments/all_games_smoke.yaml --smoke-test
```

With an existing Python 3.12 installation, replace the first line with
`python3.12 -m venv .venv`. On Windows outside WSL, activate with
`.venv\Scripts\Activate.ps1` instead. The remaining commands run in that
environment. The installer uses pip, includes `a2a-agent`, and discovers every
trusted game package declaring an `a2a_engine.environments` entry point.
`requirements-platform.txt` adds the platform and development dependencies,
including pytest; the workspace installer alone does not install the test runner.

The smoke test uses scripted agents, writes local SQLite episodes, and reads them
back. It requires no keys. `--dry-run` checks model access and can contact
providers; ordinary live runs use the configured agents. Provider-backed work is
a separate choice from installation. When requested, store keys in a private
environment or `.env`, following [AGENT_CONFIGURATION.md](AGENT_CONFIGURATION.md),
and keep them out of experiment YAML and output.

For web development, also use **Node 22** and run from `web/`:

```bash
npm ci
npm test
npx tsc --noEmit
npm run build
```

Rebuild Compose before checking the resulting UI; a host build does not update an
already-running container. See [VIEWER_EXTENSIONS.md](VIEWER_EXTENSIONS.md) for
the frontend structure and [CONTRIBUTING.md](../CONTRIBUTING.md) for checks.

## Author a game

From the repository root with the Python environment activated:

```bash
python scripts/new_game.py my-environment
python -m pip install -e games/my-environment
python -m pytest games/my-environment/tests
a2a-run games/my-environment/experiments/example.yaml --smoke-test
```

The scaffold establishes a working package, entry point, typed release/example
experiment, scripted loop, protocol spec, and tests. Replace the placeholder
rules and write `SPEC.md`; the initial smoke result is only a plumbing check.
Finish the environment declaration, executable workflow, agent/runtime adapters,
and browser support needed by your game before claiming full UI integration.
Rebuild the local stack to install the new package and inspect its catalog.

[ADDING_A_GAME.md](ADDING_A_GAME.md) is the authoring guide;
[CONTRACTS.md](CONTRACTS.md) defines trace and configuration contracts;
[episode-lifecycle.md](episode-lifecycle.md) and
[AGENT_RUNTIME_CONTRACT.md](AGENT_RUNTIME_CONTRACT.md) cover shared session
lifecycle, admission, turns, and cleanup. Use deterministic scripted tests for
rules, private observations, scoring, termination, and failure paths.

## Ask a coding agent

These prompts work by reading files in the checkout; no particular agent product
or installed skill is required. The canonical skill folder is
[`skills/groundwork-development`](../skills/groundwork-development/SKILL.md).
It can also be added to an agent's skill library using that agent's supported
installer. Its repository paths remain relative to the checkout when installed
elsewhere.

### Run locally

```text
Open my existing Project-Groundwork checkout, or clone https://github.com/devYaoYH/Project-Groundwork. Read AGENTS.md and skills/groundwork-development/SKILL.md, follow docs/INSTALLATION.md, and start the local API, worker, and runtime with Docker Compose. Use the local Alice/Bob login and run a scripted smoke experiment. Preserve existing local data and choose a free port if needed. Report the local URL and verification results.
```

### Author a game

```text
Open or clone https://github.com/devYaoYH/Project-Groundwork. Read AGENTS.md, skills/groundwork-development/SKILL.md, and docs/ADDING_A_GAME.md. Help me implement a new game: ask me for its rules, player roles, observations, scoring, and ending conditions if they are missing. Use the repository scaffold, define SPEC.md and deterministic scripted protocol tests, preserve information boundaries, and register the package. Verify a scripted smoke run and its persisted results, then rebuild the local stack and show the game in the environment catalog. Report the runnable result and tests.
```

## Hosted deployment

Local installation does not provision Google Cloud or publish a release.
[deploy/README.md](../deploy/README.md) describes the separately configured hosted
platform, Google login, immutable runtime bindings, and GitHub/Cloud Build
integration. [AGENTS.md](../AGENTS.md) records the invariants shared by local and
hosted development.
