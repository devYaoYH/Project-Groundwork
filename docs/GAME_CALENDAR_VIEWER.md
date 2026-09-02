# Calendar Game Trace Viewer

A single-page frontend for replaying and inspecting calendar scheduling game traces. Supports time-travel scrubbing through events, per-agent context window inspection, calendar state visualization, and tool call inspection.

---

## Data Model

The viewer consumes a `GameTraceBase` JSON file produced by the game engine. Key fields:

```
trace.events        — ordered list of GameEvent objects (`type`, `timestamp`, `data`)
trace.final_state   — {calendars, per_agent_cost, round_outcomes}
trace.metrics       — {coordination_rate, efficiency, fairness, ...}
trace.config        — CalendarGameConfig fields
```

Event types the viewer must handle:
`game_start`, `round_start`, `turn_start`, `turn_end`, `dm_sent`, `dm_rejected`, `decide_start`, `decide_end`, `batch_applied`, `batch_rejected`, `decision_failed`, `resolution`, `game_end`

---

## Layout

```
┌─────────────────────────────────────────────────────────────────┐
│  HEADER: game id · seed · num_agents · num_slots · metrics bar  │
├──────────────────────┬──────────────────────────────────────────┤
│                      │                                          │
│   TIMELINE PANEL     │         MAIN DETAIL PANEL                │
│   (left, ~30%)       │         (right, ~70%)                    │
│                      │                                          │
│   event list with    │   tabs: CALENDARS · CONTEXT · MESSAGES   │
│   phase grouping     │                                          │
│   + scrubber         │                                          │
│                      │                                          │
├──────────────────────┴──────────────────────────────────────────┤
│  BOTTOM BAR: current event details · phase badge · agent badge  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Panels

### Header Bar
- Game ID, seed, `num_agents × num_slots` config summary
- Summary metric pills: `coordination_rate`, `efficiency`, `fairness`, `meetings_scheduled`
- File loader button (drag-and-drop or file picker for `.json` trace)

---

### Timeline Panel (left)

The primary navigation surface. Displays all events as a scrollable list grouped by round and phase.

**Structure:**
```
▼ Round 0  [meeting #1 · agents 0,1]
  ├─ CHEAP_TALK
  │   ├─ turn 0 · agent_0 · turn_start       ← selected (highlighted)
  │   ├─ turn 0 · agent_0 · turn_end
  │   ├─ → dm agent_0 → agent_1
  │   ├─ turn 0 · agent_1 · turn_start
  │   └─ ...
  ├─ DECISION
  │   ├─ agent_0 · decide_start
  │   ├─ agent_0 · batch_applied
  │   └─ ...
  └─ RESOLUTION  ✓ coordinated / ✗ mismatch
▼ Round 1  ...
  GAME END
```

**Interaction:**
- Click any event row → jump the detail panel to that moment in time
- Keyboard: `←` / `→` arrow keys step through events one at a time
- Phase section headers are collapsible
- Color coding by phase: CHEAP_TALK = blue, DECISION = amber, RESOLUTION = green/red

**Scrubber:**
- Horizontal progress bar at top of timeline mapping event index → position
- Drag to scrub; snaps to nearest event
- Shows current event index / total

---

### Main Detail Panel (right) — three tabs

#### Tab 1: CALENDARS

Shows each agent's calendar reconstructed at the currently selected event. Calendar state is derived by replaying all `batch_applied` events up to and including the current event index.

**Per-agent calendar grid:**
```
Agent 0                          Agent 1
┌────┬────┬────┬────┬────┐      ┌────┬────┬────┬────┬────┐
│ 0  │ 1  │ 2  │ 3  │ 4  │      │ 0  │ 1  │ 2  │ 3  │ 4  │
│FREE│E#3 │ M1 │E#7 │FREE│      │ M1 │FREE│E#2 │FREE│FREE│
│    │ $1 │    │ $2 │    │      │    │    │ $1 │    │    │
└────┴────┴────┴────┴────┘      └────┴────┴────┴────┴────┘
```

Slot color legend:
- White / light grey — free
- Blue — meeting marker (labeled `M<id>`)
- Amber — errand (labeled with cost)
- Red border — slot conflict (>1 meeting in same slot, detected at resolution)
- Green border — correctly coordinated meeting slot

If the current event is a `turn_start`, highlight the calendar as it was when the agent saw it (use `turn_start.data.calendar_render` as the source of truth — no recomputation needed).

If the current event is a `decide_start`, show the frozen snapshot (`decide_start.data.calendar_snapshot_render`).

Otherwise reconstruct from replaying `batch_applied` events.

**Stats row below each calendar:**
- Displacement cost so far
- Free slots count
- Meetings placed count

---

#### Tab 2: CONTEXT WINDOW

Shows exactly what the selected agent saw as input at the current event. Source is always a logged field — no recomputation.

| Current event type | What to display |
|---|---|
| `turn_start` | `data.calendar_render` + `data.inbox_drained` rendered as messages |
| `turn_end` | `data.text` (raw model output) + `data.tool_calls` (parsed) + `data.thinking` (if present) |
| `decide_start` | `data.calendar_snapshot_render` + DECISION phase prompt |
| `decide_end` | `data.text` + `data.tool_calls` + `data.thinking` + `data.retry_count` |
| `batch_rejected` | `data.conflict_description` + `data.actions` submitted |
| other | "No context window for this event type" |

Sub-sections:
- **System prompt** — collapsible, shown once per agent (derived from game config, static)
- **Inbox messages** — numbered list from `inbox_drained`, each showing sender and content
- **Model output** — raw `text` field in a monospace block
- **Thinking trace** — collapsible, shown only if `thinking` is non-null
- **Tool calls emitted** — structured view of each tool call dict
- **Token usage** — `prompt_tokens / completion_tokens / total` if `usage` is non-null
- **Latency** — `latency_ms` if non-null
- **Raw API response** — collapsible JSON dump of `data.raw_api_response`

---

#### Tab 3: MESSAGES

Shows the DM thread for the current round, ordered by event sequence.

```
Round 0 · Meeting #1 · Participants: agent_0, agent_1

[turn 0]  agent_0 → agent_1  ──────────────────────────────
  "I'm free at slots: [4, 10, 13]. Suggest slot 4."

[turn 0]  agent_1 → agent_0  ──────────────────────────────
  "I'm free at slots: [4, 7, 11]. Slot 4 works for me."

[dm_rejected]  agent_0 → agent_1  ── dm_cap_exceeded ───────
  (message not delivered)
```

- Messages grouped by turn index
- Rejected DMs shown in red with reason
- Clicking a message row jumps the timeline to that `dm_sent` event

---

### Bottom Bar

Always visible. Shows details of the currently selected event:

```
[ CHEAP_TALK ]  [ agent_0 ]  turn_start · round 0 · turn 1 · event 7
  inbox_drained: 1 message  |  calendar_render: 16 slots  |  ↑ prev  ↓ next
```

---

## State Reconstruction

The viewer maintains a **virtual clock** driven by the selected event index. At any index:

- **Calendar state** for agent `i` = initial calendar from `game_start` scenario data, then replay all `batch_applied` events with `data.agent_id == i` at or before the selected index
- **Inbox state** = not reconstructed (use logged `inbox_drained` from `turn_start` events directly)
- **Phase** = `data.phase` of current event
- **Round** = `data.round` of current event

This means the viewer is purely a log reader — it never reruns game logic.

`GameEvent` does not yet persist a cross-game sequence field; the current Calendar
viewer assigns the array index on load. The forthcoming typed event envelope will
persist a monotonic `seq` so external viewers can retain a stable cursor after
filtering or re-exporting a trace.

---

## Metrics Sidebar (collapsible overlay)

Accessible via a button in the header. Shows:

- Per-round resolution table: meeting id, per-agent slot, coordinated?, slot conflicts
- Per-agent stats: dm_sent, dm_received, decision_retries, decision_failed, total_client_calls, displacement_cost
- Overall: coordination_rate, efficiency, fairness, meetings_scheduled, realized_cost vs optimal_cost
- Comparison bar: realized_cost vs greedy_cost vs optimal_cost

---

## Implementation Notes

**Tech stack:** single HTML file with vanilla JS + a small CSS framework (e.g. Tailwind via CDN). No build step. Viewer is a static file that can be opened directly in a browser by dragging a trace JSON onto it.

**Trace loading:**
1. Drag-and-drop `.json` file onto the page
2. Or: `?trace=path/to/trace.json` URL param for local dev server use
3. Or: inline the trace as a JS variable for embedding

**Performance:** traces are small (hundreds of events). No virtualization needed. Load entire event list into memory on open.

**Keyboard shortcuts:**
- `←` / `→` — step one event
- `[` / `]` — jump to previous/next round
- `1` / `2` / `3` — switch tabs
- `Space` — auto-play (step through events at ~2 events/sec)
- `Escape` — close any open overlay

---

## File Location

```
games/calendar/tasks/viewer.html   ← standalone viewer (already exists, to be replaced)
```

The existing `tasks/visualize.html` is a simpler prototype. The new viewer replaces it with the full design above.
