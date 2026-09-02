#!/usr/bin/env python3
"""Scaffold a new game package under games/.

    python scripts/new_game.py my-game

Writes a package that already satisfies the game contract and passes
``a2a-run ... --smoke-test`` before you have written any game logic, so the
first thing you change is the rules rather than the plumbing.

See docs/ADDING_A_GAME.md for what each generated file is for.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def to_snake(name: str) -> str:
    return re.sub(r"[-\s]+", "_", name.strip().lower())


def to_class(name: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[-_\s]+", name.strip()))


PYPROJECT = '''\
[project]
name = "{dist}"
version = "0.1.0"
description = "{title} game for a2a-engine."
requires-python = ">=3.11"
dependencies = [
    "a2a-engine",
    "expt-runner",
    "python-dotenv>=1.0",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

# Lets `a2a-run` discover the game without the caller importing it first.
[project.entry-points."a2a_engine.games"]
{snake} = "{snake}"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["{snake}"]

[tool.uv.sources]
a2a-engine = {{ path = "../../a2a-engine", editable = true }}
expt-runner = {{ path = "../../expt-runner", editable = true }}
'''

INIT = '''\
"""{title} game.

Importing this package registers the game via ``game.py``'s ``register_game``
side effect. See ``SPEC.md`` for the protocol.
"""

from {snake}.game import {cls}Config, {cls}Game

__all__ = ["{cls}Config", "{cls}Game"]
'''

GAME = '''\
"""{cls}Game — TODO: one line on what this game measures.

See ``SPEC.md`` for the protocol. Where the spec is silent on a detail, state the
choice you made here and why — the next reader needs to know which parts are the
protocol and which are your interpretation of it.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from pydantic import Field

from a2a_engine import EventLog, GameConfigBase, GameTraceBase, register_game
from a2a_engine._context import current_conversation_id
from a2a_engine.llm.factory import make_llm_client
from a2a_engine.tracing_otel import get_tracer

from {snake}.agents import {cls}Agent


class {cls}Config(GameConfigBase):
    """Config for {snake}.

    Every knob that changes model behavior belongs here: the resolved config is
    what gets recorded in the trace, and anything outside it is unrecoverable
    when someone tries to reproduce the run.
    """

    game_name: str = "{snake}"
    num_agents: int = 2
    max_turns: int = Field(default=6, ge=1)


class _ScriptedAgent:
    """Stand-in for --dry-run and --smoke-test: no LLM, no API key.

    Deterministic given the config, so a smoke test yields the same trajectory
    every time and any diff in the trace is a real behavior change.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    async def act(self, observation: dict[str, Any], tools) -> dict[str, Any]:
        with get_tracer().start_as_current_span(f"invoke_agent {{self.name}}") as span:
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", self.name)
            conv_id = current_conversation_id.get()
            if conv_id:
                span.set_attribute("gen_ai.conversation.id", conv_id)
            turn = observation["turn"]
            return {{"text": f"{{self.name}} says something on turn {{turn}}."}}


class {cls}Game:
    """TODO: describe the loop."""

    def __init__(self, config: dict | {cls}Config, dry_run: bool = False) -> None:
        self.config = config if isinstance(config, {cls}Config) else {cls}Config(**config)
        self.dry_run = dry_run
        self.events = EventLog()
        self._build_agents()

    def _build_agents(self) -> None:
        if self.dry_run:
            self.agents = [
                _ScriptedAgent(f"agent_{{i}}") for i in range(self.config.num_agents)
            ]
            return
        specs = self.config.agents
        self.agents = [
            {cls}Agent(
                make_llm_client(
                    specs[i].model_dump() if i < len(specs) else {{"model": "gpt-4o-mini"}}
                ),
                name=f"agent_{{i}}",
            )
            for i in range(self.config.num_agents)
        ]

    def run(self) -> GameTraceBase:
        # run() is the sync entry point the runner calls; the loop itself is async.
        return asyncio.run(self._run_async())

    async def _run_async(self) -> GameTraceBase:
        self.events.append("game_start", data={{"max_turns": self.config.max_turns}})

        history: list[dict[str, str]] = []
        for turn in range(1, self.config.max_turns + 1):
            for agent in self.agents:
                action = await agent.act(
                    {{"turn": turn, "history": list(history)}}, tools={{}}
                )
                text = action["text"]
                history.append({{"speaker": agent.name, "text": text}})
                # {{speaker, text}} is what makes this show up in to_messages_df()
                # and in judge transcripts.
                self.events.append(
                    "message", data={{"speaker": agent.name, "text": text}}
                )

        self.events.append("game_end", data={{"turns_used": self.config.max_turns}})

        return GameTraceBase(
            game_id=str(uuid.uuid4()),   # the runner overwrites this
            config=self.config,
            events=self.events.all(),
            final_state={{"turns_used": self.config.max_turns}},
            metrics={{
                "turns_used": self.config.max_turns,
                "messages": len(history),
            }},
        )


register_game(
    "{snake}",
    {cls}Game,
    package="{dist}",
    # The scripted agents above need no keys.
    dry_run_checks_keys=False,
)
'''

AGENTS = '''\
"""LLM agents for {snake}."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from a2a_engine import LLMAgent

SYSTEM = """\\
You are an agent in a multi-agent game. TODO: state the role, what this agent
knows, and what it must not be told about the other agents.

Reply with a single short line.\\
"""


class {cls}Agent(LLMAgent):
    def __init__(self, client, name: str = "agent") -> None:
        super().__init__(client, system_prompt=SYSTEM, name=name)

    def build_messages(
        self, observation: dict[str, Any], tools: dict[str, Callable]
    ) -> list[dict]:
        lines = [f"{{t['speaker']}}: {{t['text']}}" for t in observation.get("history", [])]
        lines.append(f"Turn {{observation['turn']}}. Your move:")
        return [
            {{"role": "system", "content": self.system_prompt}},
            {{"role": "user", "content": "\\n".join(lines)}},
        ]

    def parse_response(self, text: str, observation, tools) -> dict[str, Any]:
        # Parse forgivingly and flag failures: a trajectory costs real money, so
        # falling back beats discarding the run over a formatting slip.
        line = (text or "").strip().splitlines()
        return {{"text": line[0] if line else "", "parse_failed": not line}}
'''

ENVIRONMENT = '''\
schema_version: 1
id: {snake}.tiny
revision: v1
description: TODO — describe the world, its roles, and its task/input revision.
engine:
  game_name: {snake}
  defaults:
    num_agents: 2
    max_turns: 6
inputs: []  # Add each local task file here with path + SHA-256 before release.
roles:
  - id: participant
    count: 2
metrics:
  - name: turns_used
    producer: game
    direction: minimize
adapter_bindings:
  model: engine.llm
  communication: local.in_process
'''

EXPERIMENT = '''\
schema_version: 1
name: {snake}_example
description: TODO — what question does this experiment answer?
environment: ../environments/{snake}_tiny_v1.yaml
agents:
  - {{role: participant, type: llm, model: gpt-4o-mini}}
  - {{role: participant, type: llm, model: gpt-4o-mini}}
episodes:
  - label: baseline
    count: 2
    seeds: [1, 2]
storage:
  backend: sqlite
  path: ./results/{snake}.db
observability:
  capture_content: true
'''

RUN_PY = '''\
"""Wrapper: imports {snake} (registers the game), then defers to the expt-runner CLI.

Usage:

    uv run python run.py experiments/example.yaml --smoke-test

`a2a-run experiments/example.yaml` works too once the package is installed —
the entry point in pyproject.toml handles registration. This wrapper also loads
.env, which the bare CLI does not.
"""

import sys

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))

import {snake}  # noqa: F401,E402  (registers "{snake}" via import side-effect)
from expt_runner.run_experiment import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
'''

TESTS = '''\
"""Protocol tests for {snake}.

Scripted agents mean these need no API keys and can assert exact outcomes.
Test the rules of the game, not the behavior of a model.
"""

from __future__ import annotations

from {snake}.game import {cls}Config, {cls}Game


def run(**overrides):
    cfg = {{"max_turns": 3, "num_agents": 2, "seed": 1}}
    cfg.update(overrides)
    return {cls}Game({cls}Config(**cfg), dry_run=True).run()


def test_produces_a_trace_with_events_and_metrics():
    trace = run()
    assert trace.events
    assert trace.metrics["turns_used"] == 3
    assert trace.final_state


def test_messages_are_transcript_visible():
    """{{speaker, text}} is what makes GameDataset.to_messages_df work."""
    from a2a_engine.dataset import GameDataset

    df = GameDataset.from_traces([run()]).to_messages_df()
    assert not df.empty
    assert set(df["speaker"]) == {{"agent_0", "agent_1"}}


def test_is_deterministic_for_a_fixed_config():
    a, b = run(seed=7), run(seed=7)
    assert a.metrics == b.metrics
    assert [e.data.get("text") for e in a.events] == [e.data.get("text") for e in b.events]


def test_respects_max_turns():
    assert run(max_turns=2).metrics["turns_used"] == 2
'''

SPEC = '''\
# {title} — protocol specification

TODO: replace this outline with the actual protocol.

## 1. Roles and structural constants

Which agents exist, what each holds privately, and the fixed parameters
(rounds, budgets, discount factors).

## 2. State space

What the engine tracks between turns.

## 3. Turn order

Who acts when, and what each side observes when they act. Be explicit about
information boundaries: state what each agent must NOT be able to see. The game
enforces these by constructing observations, not by asking the model nicely.

## 4. Payoffs

The scoring function, written as a formula.

## 5. Constraints and guardrails

Rules the engine enforces, and what happens when an agent violates one — clamp,
reject, or terminate. Say which, because it changes what the trace means.

## 6. Interpretation notes

Anywhere this spec was ambiguous and you made a call, record the call and the
reasoning here.
'''


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scaffold a new a2a-comm game.")
    parser.add_argument("name", help="Game name, e.g. 'my-game' or 'my_game'")
    parser.add_argument("--games-dir", default=str(REPO_ROOT / "games"))
    args = parser.parse_args(argv)

    snake = to_snake(args.name)
    if not re.fullmatch(r"[a-z][a-z0-9_]*", snake):
        parser.error(
            f"{args.name!r} does not make a valid Python package name (got {snake!r}). "
            "Use letters, digits, hyphens and underscores, starting with a letter."
        )

    dist = snake.replace("_", "-")
    cls = to_class(snake)
    title = snake.replace("_", " ").title()

    root = Path(args.games_dir) / dist
    if root.exists():
        print(f"error: {root} already exists", file=sys.stderr)
        return 1

    fmt = {"snake": snake, "dist": dist, "cls": cls, "title": title}
    files = {
        "pyproject.toml": PYPROJECT,
        "SPEC.md": SPEC,
        "run.py": RUN_PY,
        f"{snake}/__init__.py": INIT,
        f"{snake}/game.py": GAME,
        f"{snake}/agents.py": AGENTS,
        f"environments/{snake}_tiny_v1.yaml": ENVIRONMENT,
        "experiments/example.yaml": EXPERIMENT,
        f"tests/test_{snake}.py": TESTS,
    }
    for rel, template in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(template.format(**fmt))

    print(f"Created {root}\n")
    for rel in files:
        print(f"  {rel}")
    print(f"""
Next:

  uv pip install -e {root.relative_to(Path.cwd()) if root.is_relative_to(Path.cwd()) else root}
  a2a-run {root}/experiments/example.yaml --smoke-test
  pytest {root}/tests

Then replace the placeholder loop in {snake}/game.py and write SPEC.md.
See docs/ADDING_A_GAME.md.""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
