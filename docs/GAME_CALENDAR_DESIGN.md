# Calendar Scheduling Benchmark

A multi-agent coordination environment where agents must schedule meetings by communicating privately and independently managing their own calendars. Scheduling correctness is **observed, not enforced** — it is the primary object of study.

---

## Problem Setup

Each agent holds a private calendar of `num_slots` time slots (default 16). Each slot is either:
- `None` — free
- `{"errand_id": int, "cost": int}` — occupied by an errand with a displacement cost
- `"M<id>"` — occupied by a previously scheduled meeting

Scenarios are generated **backward from a hidden witness solution**: feasible meeting slots are chosen first, then errands are filled in at target density. A set of absorbing (free) slots is guaranteed to exist so errands can always be displaced. This ensures every generated scenario is solvable in principle.

Each round introduces one **meeting** — a tuple of `(id, participants, duration=1)`. Participants must independently place the meeting on their calendars at the same slot for coordination to succeed.

---

## Round Structure

Each round proceeds in three sequential phases: **CHEAP_TALK → DECISION → RESOLUTION**.

### Phase 1: CHEAP_TALK

Agents communicate via direct messages to coordinate on a slot. Runs until quiescence (no agent emits any tool calls) or a per-agent DM cap of **100 DMs per round** is hit.

- Participants act first; non-participants can be pulled into the conversation if a participant DMs them
- Non-participants are queued once (deduped) and act after the participant pass
- Each agent's turn: drain inbox (display all received DMs in arrival order), then emit tool calls
- The only valid tool in this phase is `dm`

**Tool:** `{"type": "dm", "to": <agent_id>, "meeting_id": <id>, "content": "<str>"}`

### Phase 2: DECISION

All participants act **simultaneously**. Each agent's context is built from a frozen snapshot of their calendar taken before any decisions are applied — no agent sees another's decision. The inbox is **empty by construction** at this point (all DMs were drained during CHEAP_TALK turns) and is not included in the DECISION context.

Each agent submits a **cell** of `schedule` and `reschedule` calls which are resolved **atomically as a transaction**. The entire cell is validated together — order within the list is irrelevant. Validation checks:

- All `reschedule` source slots contain the claimed items
- All target slots are free after accounting for all other moves in the same cell (no two moves compete for the same slot)
- The `schedule` target slot is free after all reschedules in the cell are applied

If the cell is globally consistent → all mutations are applied atomically to the agent's calendar.

If not → the agent receives a conflict description and may resubmit the **entire cell** from scratch. Up to **k=3 retries** are allowed. After k failed attempts, the agent's decision is dropped and logged as `decision_failed`.

**Tools:**
- `{"type": "schedule", "meeting_id": <id>, "slot": <int>}` — place the new meeting on this agent's calendar at the chosen slot
- `{"type": "reschedule", "item_id": <id>, "from_slot": <int>, "to_slot": <int>}` — move an existing meeting or errand on this agent's calendar from one slot to another

`reschedule` is unbounded — an agent may include as many reschedules as needed to clear a slot. Both tools operate only on the calling agent's own calendar.

### Phase 3: RESOLUTION

A passive observer pass — no agent acts. The engine reads post-decision calendar state and checks:

1. **Slot conflict** — does any agent have more than one meeting assigned to the same slot?
2. **Coordination success** — for each meeting, did all participants place it at the same slot?

Outcomes are recorded as metrics. Nothing is corrected or enforced.

---

## Agent Architecture

### Two-Layer Design: Agent vs Client

**`Agent`** — stateful, environment-facing wrapper. The engine only ever talks to this layer. It owns the protocol (when to call what on the client) and holds all mutable per-agent state.

**`BaseClient`** — swappable decision-making backend. Knows nothing about the environment engine. Receives structured context, returns tool calls and metadata. Three concrete implementations are supported:

| Client type | Description |
|-------------|-------------|
| `LLMClient` | Direct LLM chat completion API (Claude, GPT, etc.). Builds prompts from `GameConfig` via `prompts.py` helpers. |
| `AgenticClient` | External agentic endpoint (Claude Code API, OpenClaw, etc.). Wraps context in endpoint-specific format. |
| `AlgorithmClient` | Classical algorithm (DCSP, MULBS, etc.). Consumes `GameConfig` directly as structured data; ignores prompt builders. |

All three satisfy the same `BaseClient` interface. The engine never needs to know which is in use.

### Agent State

```python
class Agent:
    agent_id: int
    client: BaseClient
    inbox_queue: deque        # engine appends here; agent drains on each turn
    calendar: Calendar        # mutable, owned by agent
```

`Agent.turn()` drains `inbox_queue` itself before calling `client.turn(messages)` — the flush is an internal responsibility of `Agent`, not the engine's.

### Engine-Facing Protocol (called by environment loop)

```python
agent.register(agent_id, game_config)           # environment start — once
agent.start_round(meeting, calendar_snapshot)   # round start, participant only
agent.turn() -> TurnResult                      # each CHEAP_TALK turn; drains inbox internally
agent.decide(meeting, calendar_snapshot) -> DecideResult  # DECISION phase
```

### BaseClient Interface

```python
class BaseClient:
    def register(self, agent_id: int, game_config: GameConfig) -> None: ...
    def start_round(self, meeting: dict, calendar: Calendar) -> None: ...
    def turn(self, messages: list[dict]) -> TurnResult: ...
    def decide(self, meeting: dict, calendar: Calendar) -> DecideResult: ...
```

`messages` passed to `turn()` is the already-drained, ordered inbox — the client just consumes it.

### GameConfig (replaces system_prompt)

Structured environment parameters passed to `client.register()`. LLM/Agentic clients parse this into a system prompt via `prompts.py`; `AlgorithmClient` consumes it directly.

```python
@dataclass
class GameConfig:
    num_agents: int
    num_slots: int
    agent_id: int             # this agent's identity
    participants: list[int]   # all agent ids in the environment
    dm_cap: int               # max DMs per round (default 100)
    decision_retries: int     # max retries in DECISION phase (default 3)
```

### Prompt Construction (`prompts.py`)

Prompt builders are **pure functions** — no side effects, no client state. They are the single source of truth for what text goes into every context window position.

```python
def build_system_prompt(game_config: GameConfig) -> str: ...
def build_round_start_message(meeting: dict, calendar: Calendar, round_num: int) -> str: ...
def build_turn_message(messages: list[dict]) -> str: ...
def build_decision_message(meeting: dict, calendar: Calendar) -> str: ...
def build_retry_message(retry_num: int, max_retries: int, conflict: str) -> str: ...
```

Benefits:
- **Reproducibility**: log/hash `prompts.py` per experiment run; prompt text is fully determined by inputs
- **Testability**: call directly with mock data, no client needed
- **Swappability**: test prompt variants by swapping `prompts.py` without touching client logic

### Response Types

```python
@dataclass
class TurnResult:
    tool_calls: list[dict]    # parsed tool calls; empty list = pass
    text: str | None          # raw model text or algorithm explanation
    thinking: str | None      # reasoning trace if available
    usage: TokenUsage | None  # prompt/completion token counts
    latency_ms: float | None  # wall time for the call
    raw: dict | None          # full raw API response for anything else

@dataclass
class DecideResult(TurnResult):
    retry_count: int          # retries used (0 = first attempt succeeded)
```

---

## Agent Context Windows

### A) Start of Environment — via `client.register()`
Delivered once as the static system prompt (LLM/Agentic) or structured config (Algorithm):
- `agent_id` and environment parameters (`num_slots`, `num_agents`, `dm_cap`, `decision_retries`)
- Rules: available tools, phase descriptions, objective

### B) Start of Each Round — via `agent.start_round()`
New `user` message appended to history:
- Round number
- New meeting (id, participants, duration) — participant only
- Current calendar (reflects all mutations from prior rounds)
- Drained inbox (all DMs received since last round end, in arrival order)
- Phase signal: CHEAP_TALK, only `dm` is valid

### C) Subsequent CHEAP_TALK Turns — via `agent.turn()`
New `user` message appended to history (lightweight):
- Drained inbox (new DMs since last turn, in arrival order)
- Pass prompt if inbox is empty

### D) DECISION Phase — via `agent.decide()`
New `user` message appended to history:
- Phase signal: DECISION — only `schedule` and `reschedule` are valid
- Pending meeting reminder (id, participants, duration)
- Frozen calendar snapshot

No inbox section — empty by construction.

### E) DECISION Retry (on conflict)
New `user` message appended to history:
- `[RETRY <n>/<k>]` header
- Conflict description: which validation check failed and why

Agent resubmits the entire cell from scratch.

---

## Calendar Object

```python
class Calendar:
    slots: list[Slot]         # None | Errand | Meeting, indexed 0..num_slots-1
    num_slots: int

    def is_free(self, slot: int) -> bool
    def get(self, slot: int) -> Slot
    def place(self, slot: int, item) -> None
    def move(self, from_slot: int, to_slot: int) -> None
    def snapshot() -> Calendar    # deep copy, used for DECISION context
    def render() -> str           # human-readable string for LLM context windows
```

`render()` is the single source of truth for how the calendar appears in every context window position.

---

## Engine State

### Persistent across entire environment
| State | Description |
|-------|-------------|
| `agents` | List of `Agent` objects; each owns its calendar and inbox |
| `round_outcomes` | Per-round resolution results |
| `event_log` | Append-only trace of all events |

### Per-round (reset each round)
| State | Description |
|-------|-------------|
| `cheap_talk_dm_count` | Per-agent DM count this round, enforces `dm_cap` |
| `already_queued` | Set of non-participants already pulled into CHEAP_TALK queue |
| `pending_meeting` | The meeting injected this round |

---

## Metrics & Logs

### Per-agent tracking
| Metric | When updated |
|--------|-------------|
| `dm_sent[agent_id]` | Each valid `dm` tool call |
| `dm_received[agent_id]` | Each delivery to inbox |
| `cheap_talk_turns[agent_id]` | Each CHEAP_TALK turn taken |
| `decision_retries[agent_id]` | Each DECISION retry |
| `decision_failed[agent_id]` | Decision dropped after k retries |
| `total_client_calls[agent_id]` | Every client invocation |
| `displacement_cost[agent_id]` | Errand displacement cost incurred during DECISION |

### Per-round turn schedule log
```
round 1 cheap_talk: [agent_0 (t=0), agent_1 (t=0), agent_0 (t=1), agent_1 (t=1), ...]
round 1 decision:   [agent_0, agent_1]  (simultaneous)
round 1 resolution: {meeting_1: {agent_0: slot 4, agent_1: slot 7} → FAIL (mismatch)}
```

---

## Tracing Layer

Every meaningful event is appended to an **`EventLog`** as a structured, immutable record. The log is the single source of truth for replay, debugging, and frontend visualization.

### Event Schema (target contract)

This section describes the Calendar-specific target contract. The current shared
`Event` persists an ordered list with `type`, `timestamp`, and environment-defined
`data`; the standalone viewer derives `seq` from array position. Do not treat the
fields below as a completed cross-environment wire schema until the typed event-envelope
RFC is approved and implemented.

```python
@dataclass
class Event:
    seq: int                  # monotonically increasing, total ordering across entire environment
    turn: int                 # turn index within current round (one loop over participants + queue drain)
    round: int                # round index (one meeting per round)
    phase: str                # GAME_START | CHEAP_TALK | DECISION | RESOLUTION | GAME_END
    agent_id: int | None      # None for orchestrator/resolution events
    event_type: str           # see table below
    payload: dict             # event-specific structured data
    timestamp_ms: float
```

**Turn definition:** one complete loop — all participants act once, then all queued external agents are dequeued and act. Turn index increments after each such loop within the CHEAP_TALK phase.

### Event Types and Payloads

| `event_type` | `payload` fields |
|---|---|
| `game_start` | `game_config`, `scenario_seed`, `optimal_cost`, `greedy_cost` |
| `round_start` | `round`, `meeting` |
| `turn_start` | `agent_id`, `turn`, `phase`, `inbox_drained: list[msg]`, `calendar_render: str` |
| `turn_end` | `agent_id`, `turn`, `phase`, `tool_calls_raw: list`, `text: str`, `thinking: str\|None`, `usage: dict`, `latency_ms: float`, `raw_api_response: dict` |
| `dm_sent` | `from`, `to`, `meeting_id`, `content` |
| `dm_rejected` | `from`, `to`, `reason` (e.g. `dm_cap_exceeded`) |
| `decide_start` | `agent_id`, `calendar_snapshot_render: str`, `meeting` |
| `decide_end` | `agent_id`, `tool_calls_raw: list`, `text: str`, `thinking: str\|None`, `usage: dict`, `latency_ms: float`, `raw_api_response: dict` |
| `cell_applied` | `agent_id`, `actions: list`, `calendar_render_after: str` |
| `cell_rejected` | `agent_id`, `attempt: int`, `conflict_description: str`, `actions: list` |
| `decision_failed` | `agent_id`, `attempts_exhausted: int` |
| `resolution` | `meeting_id`, `per_agent_slot: dict[agent_id, slot\|None]`, `coordinated: bool`, `slot_conflicts: dict[agent_id, list[int]]` |
| `game_end` | all summary metrics |

### Full LLM API Response

The current Calendar implementation records the complete provider response in
`raw_api_response` for calibration analyses. Local research runs deliberately
use full capture by default, including this evidence. A future reduction policy
must be backward-compatible: viewers and analysis tolerate the field being
absent, but full-capture episodes retain it.

### Why This Is Sufficient for Frontend Replay

The `seq` field gives total ordering. The frontend can:
1. **Scrub through events** by `seq` — reconstruct exact calendar state at any point by replaying `cell_applied` events in order
2. **Show message threads** — join `dm_sent` events by `meeting_id` to reconstruct per-meeting conversation threads
3. **Show per-agent LLM context** — `turn_start.calendar_render` and `turn_start.inbox_drained` are the exact strings sent to the model; no re-computation needed
4. **Show thinking episodes** — `turn_end.thinking` alongside `turn_end.tool_calls_raw`
5. **Show full API responses** — surface token usage, latency, stop reasons per turn
6. **Highlight resolution outcomes** — color-code slots by coordination success/failure using `resolution.per_agent_slot` and `resolution.slot_conflicts`
7. **Reconstruct turn ordering** — `turn` and `seq` together show exactly who acted when within each round

---

## Test Plan

Tests are organized into three layers: **unit** (no LLM, no environment loop), **integration** (full environment loop with mock clients), and **context verification** (assert on exact LLM inputs).

### Calendar Object (unit)

- `test_snapshot_isolation` — mutating a snapshot does not affect the original calendar
- `test_atomic_cell_valid` — a consistent cell (reschedule clears slot, schedule fills it) is applied fully
- `test_atomic_cell_invalid_conflict` — two moves targeting the same slot → nothing applied, conflict returned
- `test_atomic_cell_order_irrelevant` — same cell submitted in reversed order produces identical outcome
- `test_render_deterministic` — same calendar state always produces identical `render()` string

### Prompt Builders (unit, pure functions)

- `test_build_system_prompt_contains_fields` — output contains `agent_id`, `dm_cap`, `decision_retries`, all tool names
- `test_build_round_start_contains_meeting` — output contains meeting id, participants, round number, calendar render, CHEAP_TALK signal
- `test_build_turn_message_inbox_only` — output contains only the drained messages, nothing else
- `test_build_decision_no_inbox` — DECISION prompt contains no inbox section
- `test_build_retry_message` — output contains retry count and conflict description verbatim

### Protocol Correctness (integration, mock clients)

- `test_phase_ordering` — assert CHEAP_TALK turns fire before DECISION, DECISION before RESOLUTION, across event log `seq` order
- `test_dm_cap_still_gets_turns` — agent hits 100 DM cap mid-round; assert agent continues to receive turns but all subsequent `dm` tool calls are rejected and logged as `dm_rejected`; calendar and inbox of other agents unaffected by rejected calls
- `test_decision_simultaneous` — two agents both read identical pre-decision snapshots; assert neither sees the other's mutations during DECISION
- `test_decision_atomicity` — cell with internal conflict triggers no partial application; calendar state identical before and after failed cell
- `test_decision_retry_exhaustion` — agent fails k=3 times; assert `decision_failed` event logged, calendar unchanged
- `test_inbox_empty_at_decision` — after CHEAP_TALK quiescence, assert all agent inboxes are empty before DECISION phase starts
- `test_non_participant_queued_once` — participant DMs same non-participant twice in one turn; assert non-participant receives exactly one turn that round
- `test_resolution_mismatch` — agents place meeting at different slots; assert `coordinated=False` in resolution event, no calendar correction applied
- `test_resolution_slot_conflict` — agent places two meetings in same slot; assert `slot_conflicts` non-empty in resolution event
- `test_inbox_persists_across_rounds` — DM sent in round 1 that is never drained arrives at start of round 2 (edge case: agent not a participant in round 1)

### LLM Client Context Verification (integration, `CapturingClient`)

`CapturingClient` is a `BaseClient` test double that records every call's full message list without hitting an API. It returns a configurable canned response.

- `test_context_checkpoint_B` — after `start_round()`, assert captured messages contain round number, calendar render, meeting, inbox, CHEAP_TALK signal; assert nothing else present
- `test_context_checkpoint_C` — after second `turn()`, assert new message contains only inbox delta; assert calendar not re-sent
- `test_context_checkpoint_D` — after `decide()`, assert message contains meeting + calendar snapshot render, DECISION signal; assert no inbox field present
- `test_context_checkpoint_E` — after failed cell, assert retry message contains `[RETRY n/k]` and conflict description; assert prior decision message is in history
- `test_history_grows_within_round` — assert `message_history` length increases by 2 (user + assistant) each turn within a round
- `test_history_resets_between_rounds` — assert `message_history` is empty at the start of round 2's first turn

### Event Log / Tracing (integration)

- `test_event_seq_monotonic` — target test for the typed event envelope; until
  then, insertion order is the replay order
- `test_turn_index_correct` — `turn` field increments once per complete participant loop + queue drain, resets each round
- `test_raw_api_response_logged` — only for an explicit full-capture run;
  standard-capture episodes must remain replayable without it
- `test_calendar_render_in_turn_start` — `turn_start.calendar_render` matches `agent.calendar.render()` at that point in time
- `test_replay_reconstructs_state` — replaying all `cell_applied` events from the log produces the same final calendar state as `agent.calendar` at environment end

---

## Summary Metrics (Observed, Not Enforced)

| Metric | Definition |
|--------|------------|
| `coordination_rate` | Fraction of meetings where all participants chose the same slot |
| `slot_conflict_rate` | Fraction of agents with ≥1 slot collision after DECISION |
| `efficiency` | `1 - (realized_displacement_cost / optimal_cost)` |
| `fairness` | `min(per_agent_cost) / max(per_agent_cost)` |
| `meetings_scheduled` | Count of meetings with full coordination success and no slot conflicts |
