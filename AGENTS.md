# Working in Project Groundwork

Groundwork runs reproducible multi-agent experiments. Start with the user's
requested workflow; the local stack and scripted agents are the default for
setup and verification. Read [docs/INSTALLATION.md](docs/INSTALLATION.md) for
commands. The installable agent workflow is
[skills/groundwork-development/SKILL.md](skills/groundwork-development/SKILL.md).

## Code map

- `a2a-engine/`: environment/trace contracts, registry, executable workflows,
  agent-runtime protocol, event sinks, storage, and analysis.
- `a2a-agent/`: agent-side runtime, model harnesses, strategies, and history.
- `expt-runner/`: experiment expansion, attempt-scoped lifecycle, execution,
  and persistence. `a2a-run` is the shared CLI.
- `games/<name>/`: packaged environments, protocol specifications, inputs,
  experiments, replay assets, and deterministic tests.
- `cloud_platform/`: authenticated control API, owner-scoped metadata,
  infrastructure worker, and the bridge to the pinned science runtime.
- `web/`: Next.js researcher UI and public onboarding page.
- `local_stack/`: scientific compiler and original local viewer.
- `deploy/`: hosted release and verification tooling.

## Scientific and runtime invariants

Keep behavior-changing choices in resolved configuration and provenance.
Preserve experiment, cell, logical episode, attempt, and physical trace IDs;
failed attempts remain evidence and never become successful observations.

Locked experiments retain their scientific configuration and runtime source/
image pins. Current infrastructure can execute an older pinned runtime through
the bridge; it must not silently recompile its plan or run current game code in
its place. Open frozen compiler snapshots with `open_frozen_control`; normal
local control-plane startup can reconcile launches or backfill old designs.
Completion requires final evidence publication. Live logs/progress are
provisional and cannot establish scientific success.

The runner opens and closes the shared episode session. Games perform setup,
enter `session.started(registrations)`, and then execute play. Preserve admission,
capability scope, absolute deadlines, cleanup, and the distinction between an
acknowledgment and completion. Do not retry ambiguous runtime delivery or replace
unavailable seats with undeclared scripted agents. Read
[docs/episode-lifecycle.md](docs/episode-lifecycle.md) and
[docs/AGENT_RUNTIME_CONTRACT.md](docs/AGENT_RUNTIME_CONTRACT.md) when changing
these boundaries.

Construct observations explicitly so private information is absent from an
unauthorized agent's data. Keep model execution/history agent-side and
authorization, validation, state commits, and normalized events environment-side.
User-owned experiments, attempts, episodes, streams, and exports pass the API's
ownership gate; installed release declarations are shared.

## Credentials and local work

Use scripted smoke runs and deterministic protocol tests for ordinary checks.
`--dry-run` is a model-access check and can call providers; it is not a substitute
for `--smoke-test`. Use paid models or cloud deployment when the user requests
that work, rather than as an implicit consequence of installation.

Keep provider secrets in private environment configuration. The platform runtime
receives provider keys; bridge request files contain only selected runtime
settings. Never put keys, private join tickets/links, or raw credentials into
source, YAML experiments, logs, public metadata, or exported artifacts. Do not
print resolved Compose configuration when it contains secrets.

Rebuild the selected Compose stack before checking a baked-in UI. Preserve named
volumes during refresh. If another checkout is running, use a distinct project,
port, platform image tag, and runtime image tag as documented in the installation
guide.

## Authoring and checks

For a new game, use `scripts/new_game.py`, write `SPEC.md`, and follow
[docs/ADDING_A_GAME.md](docs/ADDING_A_GAME.md). Register the package entry point;
the workspace installer discovers registered packages. A scaffold proves the
minimum CLI contract; finish declarations, executable workflow, runtime
capabilities, and UI integration needed by the requested game.

Test rules, information boundaries, seeds, terminal outcomes, and cleanup with
scripted agents. Keep comparable trace events, state, and metrics stable when
refactoring; compare normalized data rather than wall-clock timestamps or fresh
trace UUIDs. Message events carry `data.speaker` and `data.text`.

Choose checks proportional to the change: focused game/protocol tests and its
SQLite smoke readback; platform ownership/bridge tests for hosting changes; web
tests, type checking, and a production build for UI changes. Run broader checks
when shared contracts or unresolved failures justify them. Report the commands
and observable results, including any limitation on validation.
