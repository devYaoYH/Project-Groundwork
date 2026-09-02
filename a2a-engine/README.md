# a2a-engine

Game-agnostic Python framework for running agent-to-agent coordination
experiments. Provides:

- An LLM HTTP client (OpenAI-compatible, Anthropic, Vertex AI) with retry,
  caching headers, streaming, and a model registry.
- Generic pydantic v2 schemas (`AgentInfo`, `GameConfigBase`, `GameEvent`,
  `GameTraceBase`) you subclass per benchmark.
- Minimal `AgentInterface` and an `LLMAgent` base class.
- YAML experiment loader with `defaults` + `batches` deep-merge semantics.
- Local JSON trace persistence.
- Threadpool fan-out helper.

Downstream benchmarks (e.g. calendar scheduling) live in their own packages
and depend on `a2a-engine`.

## Install

```bash
uv pip install -e a2a-engine
```

## Authoring a game

1. Subclass `GameConfigBase` with your game's fields.
2. Implement a game class with `run() -> GameTraceBase`. Inside, drive a
   round-robin of agents, exposing tools (e.g. `send_p2p`, `broadcast`) via
   the `tools` dict passed to `AgentInterface.act`. Append events through
   `EventLog` and return a `GameTraceBase` at the end.
3. Subclass `LLMAgent` and override `build_messages` / `parse_response` for
   your action schema.
4. Call `register_game("my_game", MyGameClass)` at import time.
5. Author an experiment YAML and run it via `expt-runner`.

## Agent interface

```python
class AgentInterface(ABC):
    @abstractmethod
    async def act(self, observation: dict, tools: dict[str, Callable]) -> dict:
        ...
```

`tools` is the only way the agent affects the world; the game owns the
implementation of each tool. Action schema is game-defined.

## Experiment YAML

```yaml
name: my_experiment
description: ...
defaults:
  game_name: my_game           # used to look up the registered game class
  num_agents: 3
  agents: [...]
batches:
  - label: baseline
    count: 5
    config:                    # deep-merged on top of defaults
      seed: 1
```

See `examples/example_experiment.yaml`.

## Analysis

### Trace-derived ratings

`a2a_engine.ratings` provides game-agnostic OpenSkill projections for
free-for-all events. A running game never updates leaderboard state. Instead,
the game declares a `rating_adapter` whose `extract(trace, artifacts)` maps a
**completed** trace to a `RatingEvent`; the engine owns replay ordering,
idempotent materialization, and snapshot construction.

Derived analyses that are expensive or evolve independently of a game run
(privacy/VPS scores, judge results, embeddings) are stored separately as a
versioned `DerivedArtifact`, keyed by the source trace digest. A stale artifact
is ignored automatically after the trace changes.

```python
from a2a_engine.ratings import rebuild_rating_snapshot
from a2a_engine.storage.sqlite import SQLiteTraceStore
from calendar_game.ratings import CalendarRatingAdapter

store = SQLiteTraceStore(path="./results/a2a_traces.db")
snapshot = rebuild_rating_snapshot(store, CalendarRatingAdapter()).snapshot
```

For a local score view, use the Compose control plane, which serves the
Calendar snapshot directly from SQLite. A future multi-node deployment can
implement the same artifact and materialization operations on shared storage;
the game trace and adapter contracts do not change.

### Dataset

Load all `GameTraceBase` JSON dumps under a results directory and get
DataFrames for analysis:

```python
from a2a_engine import GameDataset
ds = GameDataset.from_dir("results/word_guess_example")
ds.to_games_df()      # one row per game (metrics_*, final_*)
ds.to_messages_df()   # one row per message (turn, speaker, text, char_count)
ds.to_events_df()     # one row per raw event
```

### Langfuse cache

Pull remote Langfuse traces and write them as local `GameTraceBase` JSON
(lossy: `final_state`/`metrics` are not in Langfuse):

```bash
uv run python scripts/cache_langfuse.py --output-dir ./langfuse_cache \
    --tag experiment=word_guess_v1 --limit 50
```

Programmatic: `from a2a_engine.langfuse_cache import fetch_and_convert`.

### Judge prompt

LLM-as-judge prompt construction lives in the sibling [`a2a-judge`](../a2a-judge) package.

## What is intentionally not in here

- No Firestore, no FastAPI server, no websockets.
- No game-specific concepts (no resources, projects, allocations, cheap-talk).
- No CI / tests in this skeleton.
