# Calendar Game

This package contains the CalBench calendar scheduling environment. It registers
the `calendar` game with `a2a-engine`, provides task generation and oracle
solving, implements scripted/DSM/LLM agents, and writes JSON traces with
evaluation metrics.

## Engine integration contract

Calendar is the repository's canonical non-trivial integration. The live game
owns the scheduling protocol; the engine owns persistence and reporting:

1. The runner wraps the game in an OpenTelemetry root span and persists its IDs
   under `trace.observability`.
2. Calendar emits `final_state.rating_context`, a versioned deep copy of the
   scenario fields required for later scoring. A copied trace therefore does
   not need a checkout-local task file.
3. The registered `CalendarRatingAdapter` derives rating events only from
   completed traces. It never writes rating state while a model is running.
4. Expensive VPS analysis is attached later through a digest-bound artifact:

```bash
uv run python games/calendar/analysis/scripts/ingest_vps_artifacts.py \
  analysis/outputs/reflection_vps_metric/game_target_summary.csv \
  --database ./results/a2a_traces.db --rebuild
```

The local Compose stack is the supported default. It uses SQLite and no cloud
account: see [`docs/LOCAL_STACK.md`](../../docs/LOCAL_STACK.md).

## Public Fixtures

Checked-in fixtures:

| Dataset | Path | Setting |
| --- | --- | --- |
| Uniform 5a3p | `tasks/calbench_90_uniform.jsonl` | errands and meetings cost `1` |
| Varied 5a3p | `tasks/calbench_90_varied.jsonl` | errands cost `1`, `10`, or `100`; meetings cost `1` |

Each task row includes:

- `params`: agents, participants, density, slots, meetings, and cost settings
- `calendars`: private per-agent initial calendars
- `meetings`: incoming meetings and speaker order
- `witness_solution`: generated feasible solution
- `optimal`: CP-SAT oracle schedule and cost
- `greedy`: deterministic greedy schedule and cost
- `difficulty`, `difficulty_score`, and normalized difficulty metadata

## Task Generation

The task generator is implemented in `calendar_game/taskgen.py`; the calendar
constructor it calls is `calendar_game/scenario.py`. Most current fixtures should
be generated from YAML configs instead of the older built-in `--suite` presets.

Generate the main 90-task 5-agent/3-participant benchmark:

```bash
uv run python -m calendar_game.taskgen --config taskgen_configs/calbench_90_uniform.yaml
uv run python -m calendar_game.taskgen --config taskgen_configs/calbench_90_varied.yaml
```

Each half contributes 45 tasks:

```text
3 scalar density config cells x 3 CP-SAT difficulty buckets x 5 tasks per bucket
```

Together, the uniform and varied halves produce 90 tasks. Uniform uses errand
cost `1`; varied uses a balanced errand-cost multiset drawn from `[1, 10, 100]`.

### YAML Config Flow

For a custom suite, copy `taskgen_configs/example_custom.yaml`:

```yaml
setting_name: my_calendar_suite
output: tasks/my_calendar_suite.jsonl
summary_output: tasks/my_calendar_suite_summary.json
seed_base: 900000
candidates_per_config: 12
selected_per_bucket: 1
difficulty_scorer: cp_sat_solvable_assignment_fraction
configs:
  - total_agents: [3, 4]
    subset_sizes: [2, 3]
    num_slots: 16
    num_meetings: 3
    densities: [0.4, 0.7]
    pref_levels: [1, 3]
    meeting_cost_level: 1
    errand_cost_level: 3
```

Config generation proceeds in these stages:

1. Load the YAML/JSON config.
2. Merge top-level generation defaults into each item under `configs`.
3. Expand list-valued axes into concrete config cells:
   `total_agents`, `subset_size`/`subset_sizes`, `density`/`densities`,
   `pref_level`/`pref_levels`, `num_meetings`, and `num_slots`.
4. Require all expanded cells in one config file to use the same
   `num_meetings`.
5. For each expanded cell, generate `candidates_per_config` candidates.
6. Score and bucket candidates within that expanded cell.
7. Select `selected_per_bucket` tasks from each bucket.
8. Write sorted-key JSONL tasks and an optional summary JSON.

The public CalBench 90 configs expand one YAML cell into three concrete scalar
density cells: `0.6`, `0.8`, and `1.0`. The scalar `params.density` is retained
for task IDs, summaries, and per-cell difficulty bucketing. The actual per-agent
calendar loads are controlled by `params.agent_densities`.

### Candidate Construction

For each expanded config cell and candidate index, the generator creates a
candidate spec:

```text
seed = seed_base + (config_idx * 1000) + candidate_idx
task_id = candidate_{config_idx:03d}_{candidate_idx:03d}
```

If `participant_lists` is not supplied, the builder chooses participants from
the seed. For multi-meeting tasks, it uses balanced participant lists when the
number of participant appearances is divisible by the number of agents; otherwise
it falls back to random participant subsets. Speaker order is then assigned to
balance first-speaker exposure when possible.

The CalBench 90 configs enable heterogeneous per-agent load generation:

```yaml
vary_agent_densities: true
agent_density_values: [0.6, 0.8, 1.0]
vary_blocked_errands_per_agent: true
blocked_errands_per_agent_values: [2, 4, 6]
difficulty_scorer: cp_sat_solvable_assignment_fraction
```

For each candidate task, `agent_density_values` and
`blocked_errands_per_agent_values` are repeated to length `total_agents` and
shuffled with deterministic seeds derived from the candidate seed. This makes
each task heterogeneous while preventing a fixed agent index from always
receiving the same load. Explicit `agent_densities` or
`blocked_errands_per_agent` values override this automatic variation.

### Scenario Construction

`build_task()` calls `generate_scenario()` with participants, speaker order,
per-agent densities, blocked-counts, and cost settings. Scenario generation is
backward from a hidden witness solution:

1. Validate density, costs, meeting count, per-agent density list, and
   per-agent blocked-count list.
2. Create meetings with sorted participants, speaker order, duration `1`, and
   meeting costs sampled from `[1, meeting_cost_level]`.
3. Choose a hidden witness slot for each meeting such that no agent has two
   witness meetings in the same slot.
4. For each agent, compute the witness slots where that agent participates.
5. Sample blocked errands from non-witness slots.
6. Reserve enough non-witness absorbing slots to keep the witness assignment
   feasible after moving errands.
7. Force at least one movable errand onto a witness slot when density is
   positive, so the witness has non-zero displacement cost.
8. Fill the remaining movable errands randomly.
9. Compute witness, optimal, and greedy schedules.

Density is applied after accounting for the agent's witness meeting slots and
blocked slots. For an agent with `num_slots` total slots, `k` witness meeting
participations, `b` blocked errands, and density `d`, the generator places:

```text
movable_errands = round((num_slots - k - b) * d)
filled_calendar_slots = b + movable_errands
```

Blocked slots are therefore reserved first, witness meeting participation slots
are protected for feasibility, and density controls the remaining movable-errand
load.

When `errand_cost_values` is provided, as in `calbench_90_varied.yaml`, the
scenario is first generated with placeholder errand costs, then each agent's
errands receive a balanced shuffled multiset of those explicit values. The
witness, optimal, and greedy solutions are recomputed after this cost assignment.

### Difficulty Scoring

Every non-`--skip-optimal` task is scored before bucket selection. The default
scorer is:

```text
cp_sat_solvable_assignment_fraction =
  solvable_assignment_count / total_assignment_count
```

`total_assignment_count` is the number of ordered distinct meeting-slot
assignments:

```text
num_slots! / (num_slots - num_meetings)!
```

`solvable_assignment_count` is counted by asking CP-SAT to enumerate feasible
assignments in the same no-slot-reuse space. The model uses one Boolean variable
per feasible `(meeting, slot)`, requires exactly one slot per meeting, prevents
global slot reuse, and prevents an agent from being assigned to two meetings in
the same slot.

Lower fractions are harder. Candidates are sorted within each expanded config
cell by:

```text
difficulty score, then optimal_evictions, then seed
```

For `cp_sat_solvable_assignment_fraction`, scores are sorted descending before
tertile slicing, so the highest third is `easy`, the middle third is `medium`,
and the lowest third is `hard`.

Other supported difficulty scorers are:

| Scorer | Meaning |
| --- | --- |
| `cp_sat_solvable_assignment_fraction` | Fraction of ordered distinct assignments that CP-SAT can schedule; lower is harder |
| `optimal_cost_per_total_agent` | Oracle cost divided by total agents |
| `optimal_cost_per_participant_slot` | Oracle cost divided by total meeting participant-slots |
| `optimal_evictions_per_participant_slot` | Oracle displaced errands divided by participant-slots |

### Bucket Selection And Balance

After sorting candidates for one expanded config cell, the generator splits the
candidate list into easy/medium/hard tertiles and selects `selected_per_bucket`
tasks from each tertile. This preserves the final difficulty counts exactly. For
CalBench 90:

```text
3 scalar density cells x 5 selected easy tasks = 15 easy tasks
3 scalar density cells x 5 selected medium tasks = 15 medium tasks
3 scalar density cells x 5 selected hard tasks = 15 hard tasks
```

Within each CP-SAT bucket, the selector tries to preserve balanced exposure for
each agent index across the 9 `(density, blocked_count)` pairs. This matters
when agent indices are fixed to model identities in mixed-model experiments. For
a 45-task half-suite, the ideal exposure for each agent index is 5 appearances
of each pair. The selector uses this ideal as a best-effort objective while
still preserving the easy/medium/hard bucket counts; the summary records the
selected per-agent pair counts and residual imbalance.

The current per-agent pair selector evaluates all `selected_per_bucket`-sized
combinations inside a bucket. It adds each combination to tasks already selected
so far, then minimizes:

```text
max per-agent pair-count range
sum of per-agent pair-count ranges
sum squared error from ideal pair exposure
sum of candidate rank indices
candidate rank indices
```

If per-agent variation is not enabled, selection falls back to the older
blocked-count balancing path: it cycles desired blocked counts within each bucket
and falls back to the best remaining ranked candidate if a desired count is not
available.

### Validation And Output

Each generated task is validated before it is returned:

- calendar count and length match `params`
- meeting count, participant count, speaker order, and duration match `params`
- balanced multi-meeting tasks have balanced participant appearances when exact
  balance is possible
- first-speaker counts differ by at most one when participant appearances are
  balanced
- optimal, greedy, witness, difficulty, and eviction fields are present unless
  `--skip-optimal` was requested
- positive-density tasks have at least one witness-slot participant errand
- blocked-slot counts match `params.blocked_errands_per_agent`

The generator writes one JSON object per task row with sorted keys. If a summary
path is configured, it also writes a summary JSON containing config metadata,
candidate ranges, bucket counts, selected balance summaries, and difficulty
score ranges.

Use `--skip-optimal` only for quick one-off fixtures. Balanced suites require
scored tasks for difficulty bucketing; `--skip-optimal` is rejected for those
paths.

Important knobs:

| Field | Meaning |
| --- | --- |
| `total_agents` | Number of agents with private calendars |
| `subset_size` / `subset_sizes` | Participants per incoming meeting |
| `num_meetings` | Incoming meeting stream length |
| `num_slots` | Calendar horizon; public tasks use `16` |
| `densities` | Scalar config-cell density axis used for task IDs and per-config difficulty bucketing |
| `vary_agent_densities` | When true, assign each candidate a shuffled per-agent density list |
| `agent_density_values` | Density values used for per-agent lists, e.g. `[0.6, 0.8, 1.0]` |
| `pref_level` / `pref_levels` | Upper bound on randomly sampled errand costs |
| `meeting_cost_level` | Upper bound on meeting-move costs |
| `errand_cost_level` | Upper bound on errand costs when using uniform random costs |
| `errand_cost_values` | Explicit balanced errand-cost multiset, e.g. `[1, 10, 100]` |
| `blocked_errands_per_agent_values` | Candidate blocked-slot counts used before CP-SAT difficulty bucketing; defaults to `[2, 4, 6]` |
| `vary_blocked_errands_per_agent` | When true, assign each candidate a shuffled per-agent blocked-count list |
| `candidates_per_config` | Candidate pool size before per-config tertile bucketing |
| `selected_per_bucket` | Tasks retained from each easy/medium/hard bucket per config cell |
| `difficulty_scorer` | Scorer used for candidate sorting and bucket metadata |
| `seed_base` | Base seed for deterministic candidate generation |

### Tiny Mixed-Model Fixture

Generate the tiny dense varied-density smoke fixture used for mixed-model
5-agent/3-participant tests:

```bash
uv run python -m calendar_game.taskgen --config taskgen_configs/tiny_varied_density_5a3p.yaml
```

This fixture writes `tasks/tiny_varied_density_5a3p.jsonl`. It contains 9 tasks
with 5 agents, 3 participants, 1 meeting, 8 slots, and explicit per-agent
density targets drawn only from `0.6`, `0.8`, and `1.0`. The checked-in
OpenRouter smoke configs use the fixed model lineup:

```text
agent 0: qwen/qwen3-8b
agent 1: meta-llama/llama-3.1-8b-instruct
agent 2: openai/gpt-oss-20b
agent 3: qwen/qwen3-8b
agent 4: meta-llama/llama-3.1-8b-instruct
```

Across the full 9-task fixture, this gives each model balanced starting-density
exposure: Qwen and Llama each see each density 6 times, while GPT-OSS sees each
density 3 times. Single-task or two-task smoke runs are useful for checking live
model execution, but they are not a fair model leaderboard by themselves.

### Built-In And Derived Suites

The built-in `--suite` presets remain available, mostly for legacy fixtures and
probes:

| Suite | Behavior |
| --- | --- |
| `initial-small` | 12 hand-authored specs from `INITIAL_SMALL_TASKS` |
| `balanced-v1` | 3-meeting balanced grid over agent counts, subset sizes, densities, and pref levels 1 and 3 |
| `balanced-uniform-v1` | Same 3-meeting grid with pref level 1 only |
| `balanced-variable-v1` | Same 3-meeting grid with pref level 3 only |
| `balanced-uniform-5meeting-v1` | 5-meeting grid from `five_meeting_configs(preference=False)` |
| `balanced-preference-5meeting-v1` | 5-meeting grid with explicit varied errand costs `[1, 10, 100]` |
| `uniform-full` | 5-meeting full uniform-cost suite with 100 candidates per config and 4 selected per bucket |
| `varied-full` | 5-meeting full varied-cost suite with 100 candidates per config and 4 selected per bucket |
| `uniform-full-blocked` | Derive blocked-errand tasks from `tasks/uniform_full.jsonl` by marking eligible errands immovable |
| `varied-full-blocked` | Derive blocked-errand tasks from `tasks/varied_full.jsonl` by marking eligible errands immovable |
| `minimal-cost-ratio-v1` | Small probe varying errand-to-meeting move-cost ratio |
| `initial-prior-meeting-move-v1` | Probe where optimal repair reschedules an existing meeting involving an external agent |
| `derive-balanced-cost-ratios-v1` | Derive errand cost ratio suites for multipliers 2, 3, and 4 from a source JSONL |

Derived suites:

```bash
# Mark errands as immovable/blocked, then recompute feasibility and oracle costs.
uv run python -m calendar_game.taskgen --suite uniform-full-blocked --blocked-errands-per-agent 6
uv run python -m calendar_game.taskgen --suite varied-full-blocked --blocked-errands-per-agent 6

# Cost-ratio and prior-meeting repair probes.
uv run python -m calendar_game.taskgen --suite minimal-cost-ratio-v1
uv run python -m calendar_game.taskgen --suite initial-prior-meeting-move-v1
```

## Running Experiments

Dry-run smoke test with scripted clients and no API keys:

```bash
uv run python run.py experiments/example.yaml --dry-run --max-parallelism 1
```

## Headline Score

CalBench reports an optional scalar `headline_score` for compact
task/team-level comparison. It does not replace the component metrics: every
component is logged separately and should be used for diagnosis. In
heterogeneous-team runs, this trace-level `headline_score` belongs to the mixed
team on that task, not to any single model.

The default headline score is gated by task success and then uses the
equal-weighted average of three normalized axes:

```text
success_score = meetings_scheduled / num_meetings

headline_score_ungated =
  mean(cost_score, privacy_score, efficiency_score)

headline_score =
  success_score * headline_score_ungated
```

The success gate prevents failed scheduling runs from receiving a high headline
score solely because they incurred little cost, leaked little information, or
sent few messages. `success_score` is also logged separately:

```text
success_score = meetings_scheduled / num_meetings
```

Cost uses regret normalized by the greedy-to-oracle gap:

```text
greedy_normalized_regret =
  (realized_cost - optimal_cost) / (greedy_cost - optimal_cost + 1e-8)

cost_score = clip(1 - greedy_normalized_regret, 0, 1)
```

For partial-success traces, cost scoring uses a matched scheduled-only oracle
when available:

```text
scheduled_only_optimal_cost = CP-SAT oracle cost after dropping failed meetings
scheduled_only_greedy_cost = greedy cost after dropping failed meetings
cost_regret = realized_cost - scheduled_only_optimal_cost
```

The original full-task `optimal_cost`, `greedy_cost`, `oracle_per_agent_cost`,
and `per_agent_excess_burden` are still logged. The scheduled-only layer adds
`scheduled_meeting_ids`, `unscheduled_meeting_ids`,
`scheduled_only_oracle_per_agent_cost`, and
`per_agent_scheduled_only_excess_burden`. This keeps missed-meeting success
separate from displacement-cost comparison: failed meetings are reflected in
`success_score`, while cost regret compares only against the oracle burden for
meetings that were actually scheduled.

If `greedy_cost` is unavailable, CalBench falls back to oracle-normalized
regret:

```text
oracle_normalized_regret =
  (realized_cost - optimal_cost) / max(abs(optimal_cost), 1e-8)

cost_score = 1 / (1 + max(0, oracle_normalized_regret))
```

Privacy treats leakage as a penalty. When VPS or leak counts are available:

```text
normalized_privacy_leakage = vps_cost / privacy_normalizer
privacy_normalizer = max(1, total_cheap_talk_messages)  # unless provided explicitly
privacy_score = clip(1 - normalized_privacy_leakage, 0, 1)
```

If no privacy experiment or privacy metrics are present, `privacy_score` defaults
to `1.0` and `privacy_score_available` is `false`.

Communication efficiency rewards using fewer cheap-talk message actions. In new
traces this includes private DMs and groupchat actions via
`total_cheap_talk_messages`; older traces fall back to `total_dms_sent`:

```text
dm_budget_total = num_meetings * num_agents * dm_cap
message_fraction = total_cheap_talk_messages / max(dm_budget_total, 1)
efficiency_score = clip(1 - message_fraction, 0, 1)
```

If `dm_cap` is unavailable, the fallback is:

```text
message_fraction = total_cheap_talk_messages / max(total_cheap_talk_messages + meetings_scheduled, 1)
efficiency_score = clip(1 - message_fraction, 0, 1)
```

All headline components are logged as `[0, 1]` scores where higher is better:

```text
excess_cost_score = cost_score
communication_score = efficiency_score
privacy_score = 1 - normalized_privacy_leakage

normalized_excess_cost = 1 - excess_cost_score
normalized_communication_cost = 1 - communication_score
normalized_privacy_leakage = 1 - privacy_score
```

The trace also logs `headline_score_weighted`, `headline_score_ungated`,
`headline_score_weighted_ungated`, `headline_score_success_gate`, and
`headline_score_weights`, with default weights of one third each for cost,
privacy, and efficiency. Shapley-style attribution, if added, should remain a
separate diagnostic and is not part of `headline_score`.

Per-agent fields are also logged when available:

```text
per_agent_realized_cost
per_agent_oracle_cost
per_agent_excess_burden
per_agent_scheduled_only_excess_burden
per_agent_excess_cost_score
per_agent_communication_score
per_agent_dms_sent
per_agent_message_fraction
```

`per_agent_dms_sent` is currently a legacy name for per-agent cheap-talk message
actions sent; it includes groupchat actions as well as private DMs. The global
`total_dms_sent` counts private DMs only.

For model-level scoring in heterogeneous teams, aggregate from per-agent rows,
not from the trace-level headline score. A fair model comparison should balance
or adjust for `task_id`, difficulty, role, participant status, starting calendar
density, communication protocol, and teammate composition. With more models than
agent slots, use a balanced incomplete-block schedule: rotate models through the
five agent roles so each model has comparable exposure to densities, participant
roles, and teammates. New models can be added later by running them against a
frozen anchor suite and anchor models, then estimating a role-adjusted rating
from per-agent scores.

DSM baseline:

```bash
uv run python run.py experiments/uniform_full_dsm.yaml --max-parallelism 8 --resume
uv run python run.py experiments/varied_full_dsm.yaml --max-parallelism 8 --resume
```

LLM example:

```bash
uv run python run.py experiments/uniform_full_gemini31pro.yaml --max-parallelism 8 --resume
uv run python run.py experiments/varied_full_gemini31pro.yaml --max-parallelism 8 --resume
```

Useful runner flags:

| Flag | Effect |
| --- | --- |
| `--results-dir DIR` | Write traces under `DIR/<experiment_name>/` |
| `--max-parallelism N` | Number of games to run concurrently |
| `--resume` | Skip completed `experiment_run_id`s |
| `--shard-index I --shard-count N` | Partition expanded runs across machines |
| `--dry-run` | Replace agents with deterministic scripted clients |

## Experiment YAML

Minimal real-model experiment:

```yaml
name: my_openai_run
defaults:
  game_name: calendar
  task_path: tasks/calbench_90_uniform.jsonl
  num_agents: 5
  num_slots: 16
  num_meetings: 5
  num_participants: 3
  max_turns_per_round: 15
  decision_retries: 3
  enable_fallback: false
  agents:
    - {type: llm, model: gpt-4o-mini}
    - {type: llm, model: gpt-4o-mini}
    - {type: llm, model: gpt-4o-mini}
    - {type: llm, model: gpt-4o-mini}
    - {type: llm, model: gpt-4o-mini}
batches:
  - label: b037_easy_5a_3p_d0p6_c1_1
    count: 1
    config:
      seed: 323032
      task_id: b037_easy_5a_3p_d0p6_c1_1
```

Agent types:

| Type | Purpose |
| --- | --- |
| `scripted` | deterministic smoke-test behavior; automatically used with `--dry-run` |
| `dsm` | distributed score-based multi-round baseline |
| `paper_dsm` | paper-style privacy-preserving DSM |
| `private_dsm` | high-privacy DSM preset |
| `sd` | scheduling-difficulty baseline |
| `imap` / `incremental_map` | complete-information incremental MAP baseline |
| `llm` | standard prompt with provider-backed LLM calls |
| `dspy` | prompt-variant-backed LLM client |

LLM and DSPy agents default to `temperature: 0.0` when the agent spec omits a
temperature. Experiments may still set `temperature` explicitly when a
non-deterministic run is desired.

Mixed-team and protocol knobs:

```yaml
defaults:
  game_name: calendar
  num_agents: 5
  communication_protocol: all         # dm, participant_groupchat, all_groupchat, or all
  agent_densities: [0.6, 0.8, 1.0, 0.6, 0.8]  # optional per-agent calendar density
  agents:
    - {type: llm, model: gpt-4o-mini}
    - {type: llm, model: claude-sonnet-4-5-20250929}
    - {type: llm, model: meta-llama/llama-3.1-8b-instruct}
    - {type: dsm}
    - {type: sd}
```

Cheap-talk channels:

| Tool | Audience |
| --- | --- |
| `dm` | One private recipient, set with `to` |
| `participant_groupchat` | Current meeting participants only |
| `all_groupchat` | Every agent in the task, including non-participants |

Backward-compatible protocol aliases are still accepted: `groupchat` means
`all_groupchat`, and `dm_and_groupchat` means `dm` plus `all_groupchat`.
Participants are active by default at the start of each meeting round.
Non-participants become active only after receiving a DM or an all-agent
groupchat. Participant-only groupchat does not activate non-participants.
Agents may send multiple communication actions in a single CHEAP_TALK turn, so
when the protocol enables both private DMs and groupchat, one response may
contain both action types.

Traces record `team_model_counts`, `is_heterogeneous_team`,
`representation_elo_by_agent`, and per-agent `contribution_scores`. The agent
CSV export includes `contribution_score`, `density_adjusted_contribution_score`,
`calendar_density`, and `representation_elo`.

Provider credentials:

| Provider | Model examples | Credential |
| --- | --- | --- |
| OpenAI | `gpt-4o-mini`, `o3-mini` | `OPENAI_API_KEY` |
| Anthropic | `claude-sonnet-4-5-20250929` | `ANTHROPIC_API_KEY` |
| Google Gemini API | `gemini-2.0-flash` | `GOOGLE_API_KEY` |
| Vertex AI | `publishers/google/models/gemini-...` | Google ADC |
| OpenRouter | `meta-llama/llama-3.3-70b-instruct` | `OPENROUTER_API_KEY` |
| Ollama | `llama3`, `qwen...` | local Ollama server |

For custom OpenAI-compatible endpoints, set `api_format`, `api_base`, and
`api_key` directly on each agent entry.

## Extra Experiment Families

Privacy probe:

```bash
uv run python run.py experiments/privacy_probe_nosy_agent0_sample.yaml --max-parallelism 2
```

Adversarial red-team prompts:

```bash
uv run python run.py experiments/redteam_c006_uniform_5a3p_c020.yaml --max-parallelism 8 --resume
uv run python run.py experiments/redteam_c006_varied_5a3p_c020.yaml --max-parallelism 8 --resume
```

Blocked-calendar scheduling:

1. Generate blocked tasks with `calendar_game.taskgen`.
2. Copy an experiment YAML.
3. Set `task_path` to the blocked JSONL.
4. Run the same way as standard fixtures.

## Evaluation

Print grouped metrics:

```bash
uv run python -m calendar_game.evaluate results/uniform_full_dsm
```

Export analysis tables:

```bash
uv run python -m calendar_game.evaluate results/uniform_full_dsm \
  --summary-csv results/uniform_full_dsm/summary.csv \
  --game-csv results/uniform_full_dsm/games.csv \
  --round-csv results/uniform_full_dsm/rounds.csv \
  --agent-csv results/uniform_full_dsm/agents.csv \
  --message-csv results/uniform_full_dsm/messages.csv
```

Programmatic loading:

```python
from calendar_game.dataset import CalendarGameDataset

ds = CalendarGameDataset.from_dir("results/uniform_full_dsm")
game_df = ds.to_game_df()
round_df = ds.to_round_df()
agent_df = ds.to_agent_df()
message_df = ds.to_message_df()
```

Key evaluation columns:

- `coordination_rate`: scheduled meetings divided by attempted meetings
- `realized_cost`, `optimal_cost`, `excess_cost`, `cost_ratio`
- `oracle_per_agent_cost`, `per_agent_excess_burden`, and `total_excess_burden`
- `total_dms`, `total_groupchat_messages`, `total_participant_groupchat_messages`, `total_all_groupchat_messages`, `msgs_per_meeting`, `dm_chars_per_meeting`
- `cost_gini`, `fairness_metric`, per-agent `cost_share`, and contribution columns

Reusable paper-analysis scripts live in `analysis/scripts/`.

## Tests

```bash
uv run pytest calendar_game/tests/
```
