export const repositoryUrl = "https://github.com/devYaoYH/Project-Groundwork";

export const installationPrompts = [
  {
    id: "local-stack",
    title: "Run locally",
    description: "Start the control pane, worker, and episode runtime on your machine.",
    prompt: `Open my existing Project-Groundwork checkout, or clone ${repositoryUrl}. Read AGENTS.md and skills/groundwork-development/SKILL.md, follow docs/INSTALLATION.md, and start the local API, worker, and runtime with Docker Compose. Use the local Alice/Bob login and run a scripted smoke experiment. Preserve existing local data and choose a free port if needed. Report the local URL and verification results.`,
  },
  {
    id: "new-game",
    title: "Author a game",
    description: "Turn your game rules into an environment with reproducible experiment evidence.",
    prompt: `Open or clone ${repositoryUrl}. Read AGENTS.md, skills/groundwork-development/SKILL.md, and docs/ADDING_A_GAME.md. Help me implement a new game: ask me for its rules, player roles, observations, scoring, and ending conditions if they are missing. Use the repository scaffold, define SPEC.md and deterministic scripted protocol tests, preserve information boundaries, and register the package. Verify a scripted smoke run and its persisted results, then rebuild the local stack and show the game in the environment catalog. Report the runnable result and tests.`,
  },
] as const;

export const installationCommands = `git clone ${repositoryUrl}.git
cd Project-Groundwork
docker compose -f compose.platform.yml up --build --detach --wait
docker compose -f compose.platform.yml ps
curl --fail http://127.0.0.1:8080/api/health`;
