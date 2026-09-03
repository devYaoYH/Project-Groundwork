# LLM Judge: Coordination Pattern Discovery

Analyzes multi-agent negotiation environment episodes to inductively discover communicative and strategic patterns that predict coordination success or failure.

## Pipeline Overview

Each cell run does two LLM phases plus deterministic aggregation:

```
Phase 0: Extraction    — pull trace → NegotiationGame → JudgeGameContext per environment
                         (saves one JSON per environment for inspection)
Phase 1: Discovery     — one LLM call per environment, judge sees all rounds at once
                         output: per-round patterns + prior_round_influence + game_attribution
Phase 2: Consolidation — one LLM call over all environment judgments
                         output: taxonomy with up to 10 failure_modes + 10 positive_patterns
Aggregation            — flatten raw environment judgments into a CSV
```

```
experiment_traces.json
  → extractor.py (filter V5+, derive outcomes, build JudgeGameContext, mark shifting role)
    └─ writes output/extracted/<episode_uid>.json
  → judge.py (one LLM call per environment → free-text patterns, per-round breakdown)
    └─ writes output/raw/<episode_uid>.json
  → consolidate.py (cluster discovered names → taxonomy.json)
  → aggregate.py (flatten → judgments.csv)
```

## Quick Start

```bash
# 1. Ensure episodes are cached
uv run python scripts/cache_experiment_data.py --full

# 2. Dry run (no API calls — writes extracted contexts only, verify data extraction)
uv run python -m judge.run_cell --dry-run

# 3. Run full cell pipeline
uv run python -m judge.run_cell \
  --provider anthropic \
  --model claude-sonnet-4-5-20250929

# 4. Interactive mode (single environment)
uv run python -m judge.run_interactive \
  --environment-id <episode_uid_or_prefix> \
  --provider anthropic
```

## CLI Options

### `run_cell.py`

| Flag | Default | Description |
|------|---------|-------------|
| `--provider` | `anthropic` | LLM provider (`anthropic`, `openai`, `gemini`, `openrouter`) |
| `--model` | provider default | Judge model override (or set `JUDGE_MODEL` env var) |
| `--output-dir` | `judge/output/` | Where to write results |
| `--temperature` | `0.0` | LLM temperature |
| `--limit N` | all | Judge only the first N games (for testing) |
| `--no-consolidate` | | Skip taxonomy consolidation (CSV still emitted from raw) |
| `--resume` | on | Skip games that already have a saved raw judgment |
| `--dry-run` | | Extract contexts only, no API calls. Still writes `extracted/*.json`. |

### `run_interactive.py`

| Flag | Default | Description |
|------|---------|-------------|
| `--environment-id` | required | Environment ID (or prefix match) |
| `--provider` | `anthropic` | LLM provider |
| `--model` | provider default | Judge model |
| `--taxonomy` | auto-detect | Path to `taxonomy.json` (if present, aliases are shown) |

## Output Structure

```
judge/output/
  extracted/                    # Phase 0: JudgeGameContext per environment (what the LLM will see)
    <episode_uid>.json
  raw/                          # Phase 1: one JSON per GAME (all rounds bundled)
    <episode_uid>.json
  taxonomy.json                 # Phase 2: ≤10 failure_modes + ≤10 positive_patterns
  judgments.csv                 # One row per environment, rounds_summary as JSON column
```

### `extracted/` — Phase 0: Extracted Contexts

One JSON per environment containing the exact `JudgeGameContext` that the judge will see: environment metadata, shifting-role info, oracle stats, and for each round the transcript (thinking + speech), allocations, rewards, and auto-allocated flags. Useful for inspecting inputs before spending API tokens and for reproducibility across runs.

### `raw/` — Phase 1: Free Discovery

One JSON per **environment** (not per round). Contains:
- Per-round breakdowns with free-text `patterns` (effectiveness, intent, evidence quotes, coherence)
- `prior_round_influence` on each round (cross-round dynamics: repair, learning, regression)
- `game_attribution` — a 2–3 sentence arc across the whole environment

Files are written immediately after each judge call for crash safety. On re-runs, games with an existing raw judgment are skipped (resume support).

### `taxonomy.json` — Phase 2: Consolidation

Single file produced after all games are judged. Collects every unique pattern name from `raw/` and clusters synonyms into two buckets:
- `failure_modes` — up to 10 negative coordination patterns
- `positive_patterns` — up to 10 positive coordination patterns

Each canonical pattern has `id`, `label`, `description`, `aliases` (original names it subsumes), and a `context_dependent` flag for patterns whose valence flips based on context. Neutral is dropped at the canonical level.

### `judgments.csv` — Per-Environment Breakdown

**One row per environment.** Each row has flat environment-level columns plus a single `rounds_summary` JSON column that nests per-round pattern breakdowns. This format matches the analysis question "what patterns drive success/failure per environment, broken down by round?" and is directly consumable by the M/C ratio failure-mode chart.

Columns:

| Category | Column | Type |
|---|---|---|
| Identity | `episode_uid`, `model_a`, `model_b` | str |
| Conditions | `mode`, `shifting_agent` (or None for stable), `mc_ratio`, `oracle_optimum` | mixed |
| Structure | `num_rounds`, `avg_joint_efficiency` | int/float |
| Environment-level totals | `total_positive_instances`, `total_negative_instances`, `total_neutral_instances` | int |
| Unique canonicals (environment) | `unique_failure_modes`, `unique_positive_patterns` | JSON list[str] |
| Per-round breakdown | `rounds_summary` | JSON list[dict] (see below) |
| Narrative | `game_attribution` | str |

**`rounds_summary` schema** — one dict per round, sorted by round_number:

```json
[
  {
    "round_number": 1,
    "round_outcome": "suboptimal",
    "joint_efficiency": 0.9,
    "n_positive_instances": 4,
    "n_negative_instances": 2,
    "n_neutral_instances": 0,
    "failure_modes": [
      {"name": "Anchoring with Dominant Strategy", "canonical_id": "anchoring",
       "effectiveness": "negative", "intent": "self-interested"}
    ],
    "positive_patterns": [
      {"name": "Transparent Disclosure", "canonical_id": "transparent_disclosure",
       "effectiveness": "positive", "intent": "cooperative"}
    ],
    "other_patterns": [
      {"name": "Unclassified thing", "canonical_id": "other",
       "effectiveness": "neutral", "intent": "ambiguous"}
    ],
    "round_attribution": "R1 story.",
    "prior_round_influence": null
  },
  { "round_number": 2, ... }
]
```

**How canonical_id is assigned** (no extra LLM call):

1. Phase 2 consolidation produces a `Taxonomy` with `aliases` lists per canonical pattern.
2. `aggregate.py::build_alias_index()` builds a case-insensitive `{alias → canonical_id + bucket}` map.
3. Each round's free-text `pattern_name` is looked up in this map and placed into `failure_modes`, `positive_patterns`, or `other_patterns` accordingly.

Patterns whose name doesn't match any alias are tagged `canonical_id: "other"` and placed in `other_patterns`.

**Joining with other DataFrames**: use `episode_uid` as the join key. For per-round analysis, flatten `rounds_summary` in your notebook via `df.rounds_summary.apply(json.loads).explode()`.

## Key Output Fields Explained

### Narrative fields (written by the LLM judge)

**`game_attribution`** — A 2-3 sentence narrative summarizing the entire environment arc: what happened across all rounds, the key turning points, and overall coordination quality. Written after the judge sees all rounds together, so it can identify themes that no single round shows on its own (e.g., path dependence, cumulative trust-building, or progressive regression). Example:

> *"This stable-partner environment exhibited strong path dependence, with Round 1's negotiated 3/3 starlight split becoming a locked-in equilibrium that both rounds replicated exactly. Agent A successfully established dominance through conditional commitment tactics, securing 12 reward in both rounds while Agent B accepted 6 reward despite having a theoretical path to 12. Early coordination success paradoxically prevented discovery of better equilibria."*

**`round_attribution`** — A 1-2 sentence explanation of why a specific round succeeded or failed, grounded in the patterns discovered for that round and the oracle stats (what was optimal vs what was achieved). Example:

> *"The round achieved 90% efficiency because Agent A negotiated a 3/3 starlight split that maximized their own payoff (12) while Agent B accepted a suboptimal allocation (6) to avoid annulment risk."*

**`prior_round_influence`** — How prior rounds shaped this round's outcome. Only present on rounds 2+. Captures cross-round dynamics that single-round analysis would miss: repair after overdraw, learning from failure, regression despite earlier success, or precedent lock-in. `null` for round 1 (no prior context). Example:

> *"Strong path dependence from Round 1. Agent A explicitly invoked Round 1's 'success' to justify repeating the same split. Agent B learned that resistance was futile and accepted more quickly."*

### Per-pattern fields (one per discovered pattern per round)

**`name`** — Free-text label the judge invents inductively (e.g., "Path-Dependent Lock-in", "Transparent Preference Revelation"). Not from a fixed taxonomy; every environment can produce different names. Mapped to canonical IDs during aggregation via alias lookup.

**`description`** — What the pattern is and how it manifested in this specific round. Includes concrete details from the transcript.

**`effectiveness`** — Did this pattern help or hurt the round's outcome? Values: `positive`, `negative`, `neutral`. Assessed per-instance (the same canonical pattern can be positive in one round and negative in another if it's `context_dependent`).

**`intent`** — Was the agent's behavior cooperative or self-interested? Values: `cooperative`, `self-interested`, `ambiguous`. Note: a self-interested pattern can still have positive effectiveness (e.g., Agent A's anchoring maximized their reward but also avoided annulment).

**`speech_allocation_coherent`** — Did the agent's public speech match what they actually submitted as their allocation? `false` flags cases where an agent said one thing and did another (e.g., agreed to a split verbally but submitted a different allocation).

**`coherence_note`** — When `speech_allocation_coherent` is `false`, this explains the gap (e.g., "Agent B advocated for 6 starlight but submitted 3 after negotiation pressure").

**`evidence`** — List of direct quotes from the transcript supporting this pattern. Each quote is tagged with `speaker` (agent_a/agent_b) and `type` (thinking/speech/allocation).

### Environment condition fields (from the input data, not LLM-generated)

**`mode`** — `stable` (both agents keep full conversation history across rounds) or `shifting` (one agent's context resets each round).

**`shifting_agent`** — Which agent has its context reset each round: `agent_a`, `agent_b`, or `null` for stable games. Derived from the environment's `swapped` config flag. The judge prompt explicitly tells the LLM which agent is shifting so it doesn't miscode a fresh-partner response as regression.

**`mc_ratio`** — Goal compatibility (M/C ratio). `0.5` = highly competitive, `0.8` = moderate, `1.0` = fully collaborative. Determines how much joint surplus is available through coordination vs individual play.

**`oracle_optimum`** — The theoretical maximum joint reward achievable by perfectly coordinating allocations (computed by the environment engine's solver). `joint_efficiency = actual_joint_reward / oracle_optimum`.

**`round_outcome`** — Derived from ground truth: `overdrawn` (total resource demand exceeded supply), `optimal` (joint efficiency >= 99.9%), or `suboptimal` (everything else). Not set by the LLM; forced from the data during parsing.

## Module Reference

| Module | Responsibility |
|--------|---------------|
| `schema.py` | Pydantic models: `JudgeGameContext`, `GameJudgment`, `Taxonomy` (split into `failure_modes` + `positive_patterns`), etc. |
| `prompts.py` | System + user prompt templates for Phase 1 and Phase 2 |
| `extractor.py` | `NegotiationDataset` → `JudgeGameContext` (V5+ filter, baseline skip, shifting-agent derivation, per-round oracle for rotating games) |
| `judge.py` | `judge_game()` — one LLM call per environment, Pydantic validation, retry-with-error-feedback, ground-truth enforcement |
| `consolidate.py` | Collect unique patterns, cluster into two-bucket taxonomy |
| `aggregate.py` | Flatten environment judgments to CSV; deterministic alias → canonical_id mapping |
| `run_cell.py` | Cell entrypoint with extraction caching, resume, crash-safe writes, `--dry-run` |
| `run_interactive.py` | Single-environment rich stdout output |

## Design Decisions

- **One LLM call per environment** (not per round) so the judge can identify cross-round dynamics like repair, learning, regression, and path dependence.
- **Inductive discovery**: the judge receives no fixed taxonomy in Phase 1. Consolidation happens separately in Phase 2 with a few-shot example that teaches merging patterns.
- **Two-bucket taxonomy** (failure vs positive): matches the analysis question "what predicts success vs failure?" and makes the failure-mode breakdown chart trivial to build.
- **Shifting role surfaced in the prompt**: for `shifting` games, the judge is told which agent's context resets each round, so it doesn't miscode a fresh-partner response as regression/defection.
- **Temperature 0.0**: consistency across runs. The `extracted/` cache + raw judgment resume make the pipeline fully reproducible.
- **V5+ only**: earlier schema versions lack project-based rewards and oracle stats.
- **Thinking episodes included**, truncated to 1500 chars per turn, labeled as private. Enables intent analysis without blowing up prompt size.
- **Auto-allocation flagged**: rounds where the engine assigned projects greedily (not the agent's choice) are explicitly called out so the judge doesn't misattribute.
- **Ground-truth enforcement**: `episode_uid`, `round_number`, `round_outcome`, and `joint_efficiency` in the LLM's output are always overridden from the context, preventing hallucinated metadata.
- **Deterministic canonical mapping**: after Phase 2 produces a taxonomy with `aliases` per canonical pattern, the CSV builder uses a case-insensitive alias lookup to map free-text pattern names to canonical IDs — no extra LLM call needed.

## Tests

```bash
uv run pytest tests/test_judge.py -v
```

47 tests covering schema validation (two-bucket taxonomy, valence lookup, context_dependent flag), prompt construction (stable/shifting callout, auto-allocation, transcript rendering), data extraction (V5+ filter, baseline skip, shifting-agent detection for both swapped and non-swapped games), JSON parsing with ground-truth enforcement, consolidation, and per-environment CSV aggregation (alias matching, counts, round summaries, no-taxonomy fallback).
