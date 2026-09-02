# Experiments

## Quick Start

```bash
# 1. Start the server
uv run python -m backend.server

# 2. Dry run (heuristic agents, no Firestore)
uv run python scripts/run_experiment.py experiments/no_talk_baseline.yaml --dry-run

# 3. Real run (LLM agents, saves to Firestore)
uv run python scripts/run_experiment.py experiments/full_factorial.yaml
```

## Files

| File | Description |
|---|---|
| `presets.yaml` | Reusable game configs (agents, resources, rounds, etc.) |
| `no_talk_baseline.yaml` | 4 batches: no cheap talk across all mode/goal combos |
| `talk_baseline.yaml` | 4 batches: with cheap talk across all mode/goal combos |
| `full_factorial.yaml` | 8 batches: talk x no-talk x 4 mode/goal combos |

## Config Resolution

Each batch's final config is built by merging (later wins):

1. **Preset base** (`extends` parent, if any)
2. **Preset fields**
3. **Experiment `defaults`** (any GameConfig field)
4. **Per-batch `config`** (any GameConfig field)

Batches can override `preset` to use a different base than the experiment default.

## Runner Flags

| Flag | Effect |
|---|---|
| `--dry-run` | Swaps agents to heuristic, disables Firestore |
| `--server URL` | Target server (default: `http://localhost:8080`) |
