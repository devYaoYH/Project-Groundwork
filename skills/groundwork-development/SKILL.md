---
name: groundwork-development
description: Set up Project Groundwork locally or implement a requested multi-agent game using its scaffold, scientific contracts, and deterministic verification. Use for Groundwork onboarding and game development; hosted deployment is a separate requested workflow.
---

# Groundwork development

Locate the user's existing Project Groundwork checkout, or clone
`https://github.com/devYaoYH/Project-Groundwork` into a suitable working directory
when needed. All paths below refer to that checkout, including when this skill
has been installed elsewhere. Read its `AGENTS.md` before editing.

## Local setup

Follow `docs/INSTALLATION.md`. Prefer the authenticated local platform stack in
`compose.platform.yml`: API, infrastructure worker, and separate science runtime.
Use its Alice/Bob development identities and scripted smoke mode; no cloud
account or provider key is required. The original `docker-compose.yml` viewer
is a distinct alternative, not an overlay on the platform stack.

Inspect an existing deployment before choosing ports/project/image tags. Preserve
data volumes and private configuration. Build the current images, wait for the
API health check, and verify actual worker execution, persisted evidence, joined
export, and owner isolation with the documented local checker. Report the URL
and results. A healthy API alone does not prove the runtime worked.

For Python development use the guide's seeded Python 3.12 environment and
`scripts/install_workspace.py`; it installs core packages and discovers game
entry points. Host Node 22 is needed only for editing/building `web/`, not for the
container quickstart.

## Implement a game

Use the user's rules, roles, observations, scoring, and termination conditions
to write `games/<name>/SPEC.md`. Resolve consequential missing rules with the user;
record routine interpretation choices in the spec while progressing on
independent work.

Follow `docs/ADDING_A_GAME.md` and `docs/CONTRACTS.md`. Run
`scripts/new_game.py <name>` and inspect the generated package and smoke run
before replacing its placeholders. The scaffold is a minimal CLI starting point;
complete the declarations, workflow, adapters, and UI support the task requires.
Use the shipped games as executable references rather than copying a second
runner or lifecycle implementation.

Keep model-behavior choices in configuration, exclude private facts from
unauthorized observations, and emit normalized events and final metrics.
For agent-runtime integration read `docs/episode-lifecycle.md` and
`docs/AGENT_RUNTIME_CONTRACT.md`; the shared runner owns session setup/teardown,
while the game owns its play, permissions, and state resolution.

Install the new entry-point package, run its deterministic protocol tests, and
execute a scripted SQLite smoke test with readback. Extend relevant catalogue,
workflow, runtime, and browser checks when the game needs those surfaces. Preserve
immutable scientific identities and frozen runtime pins when modifying existing
experiments. Use `CONTRIBUTING.md` for the contribution checklist.

## Scope and handoff

Local setup/game authoring does not imply a paid provider run or cloud release.
Honor a later explicit request for those workflows; consult `deploy/README.md`
for hosted operations. Keep secrets and private join material out of output and
artifacts. Finish with the runnable result, exact verification commands/results,
and any remaining limitation.
