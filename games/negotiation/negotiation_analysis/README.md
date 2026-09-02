# Data Analysis

Analysis scripts for the A2A Negotiation research project.

## Setup

**Important:** Data files are not tracked in git. Generate them locally:

```bash
# Cache experiment data from Firestore → data/experiment_traces.json
uv run python scripts/cache_experiment_data.py

# Export traces to CSV (optional)
uv run python scripts/export_traces.py
```

## Primary Data Source

The project uses `data/experiment_traces.json` (30 labeled experiment games cached from Firestore). This is the canonical dataset for analysis.

**Do not** load data from `data/games/` (deprecated, stale data).

## Running Analysis Scripts

All analysis scripts are standalone modules that can be run independently:

```bash
# Run individual research question analysis
uv run python -m scripts.analysis.rq1_speech_compression
uv run python -m scripts.analysis.rq2_common_ground_repair
uv run python -m scripts.analysis.rq3_overdraw_prevalence
uv run python -m scripts.analysis.rq4_value_sharing
uv run python -m scripts.analysis.rq5_global_optimum
uv run python -m scripts.analysis.rq6_stated_vs_actual
uv run python -m scripts.analysis.rq7_strategy_taxonomy
uv run python -m scripts.analysis.rq8_sycophancy
uv run python -m scripts.analysis.rq9_centering_theory
```

Each script returns a dictionary containing:
- DataFrames with processed results
- Summary statistics
- Metadata about the analysis

## Jupyter Notebooks

The `notebooks/research_questions.ipynb` notebook provides visualization for all research questions. It imports analysis logic from these scripts rather than duplicating code.

## Data Loading

Always load experiment data via the data loader:

```python
from scripts.analysis.data_loader import load_experiment_data, build_round_df, build_turn_df

# Load raw game data (tries Firestore, falls back to cached JSON)
games = load_experiment_data()

# Build analysis DataFrames
round_df = build_round_df(games)
turn_df = build_turn_df(games)
```

## Experiment Data Schema

The 30 labeled experiment games have structured condition labels following this format:

```
{mode}_{goal_type}_{thinking_mode}[_no_outcome]
```

**Dimensions:**
- `mode`: stable (18 games) / shifting (12 games)
- `goal_type`: collaborative (14) / competitive (16)
- `thinking_mode`: hidden_thinking (18) / shown_thinking (12)
- `outcome_visibility`: implicit (18) / no_outcome (12)

**Important notes:**
- Cheap talk transcript turns are **0-indexed**
- Transcript entries have `speaker` values: `"agent_a"`, `"agent_b"`, or `"system"` — filter to agents when analyzing agent behavior
- In shifting mode, agent_a retains context across rounds while agent_b's context resets
- Agents can submit early via `[DECISION]` in their message

See [CLAUDE.md](../../CLAUDE.md) for detailed development and data analysis guidelines.
