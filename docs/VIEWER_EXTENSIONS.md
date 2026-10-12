# Episode viewers

The **standard episode view** is what every environment gets out of the box, with no front-end work: one lane per participant, a scrubber, a full-width chat transcript, and the generating execution config disclosed. A new environment is inspectable on the day it first runs.

Environment-owned replay assets under `games/<environment>/replay/` remain available as standalone pages. Specialised replay viewers are not currently mounted on the Episode screen; the web app has no replay registration or iframe plumbing.

## The lane projection

`web/lib/lanes.ts` is a pure function over the trace. It is what turns a flat
event list into rows, marks and index separators, and it is shared by anything
that wants the same reading of an episode.

```ts
projectLanes(events, lanes, indexLabel) -> { rows, separators, cursorMax }
```

Three conventions decide what it produces, and an environment gets the standard
view for free by following them:

| Convention | Effect |
|---|---|
| `event.data.speaker`, else `event.data.participant_id`, else `event.data.agent` | exact roster id, then unique role, then discovered lane; named attribution precedes Calendar's non-negative integer `agent_id` fallback to roster seat or `agent_N`, otherwise `system`. An explicitly recorded `speaker: "system"` wins over `agent`. |
| `event.data.text`, else legacy `event.data.content` | the event reads as an utterance rather than a committed action, and appears in the transcript |
| `event.data[index_label]` | an index separator is drawn where the value changes |

`index_label` is not guessed. It comes from the release declaration: a measure
declared `grain: "sequence"` names the environment's own word for position
inside an episode (`round`, `turn`, `slot`). A release that declares none gets a
continuous lane, which asserts nothing false — unlike, say, rendering every
message as public when the environment never said it was.

Colour carries participant identity only, uniformly, and only within one view.
Saturation is deliberately unused: encoding visibility with it would claim every
message was broadcast, which is wrong for an environment that has private
messages and does not tag them. See design discussion §17.

Every input is treated as optional. An episode with no events, a trace with no
roster, an event with no payload and a payload with no speaker are all real
states, and `lib/lanes.test.ts` pins each of them.

## Chat transcript conventions

`projectChat(projectTranscript(events, roster), projectLanes(events, roster, indexLabel).rows, roster)` preserves one row for every recorded event, including silent events, with the original cursor, lane identity, text and payload.

- Participant text renders as tinted message bubbles; participant events with absent or empty text render as compact action chips.
- System text renders as centred notices; silent system events render as centred dividers labelled with their event type (and recorded round when present).
- Group mode applies when there are more than two distinct valid non-system roster ids or more than two speaking lanes. Otherwise the first speaking lane in Lanes order goes left and the second goes right. Group participants and participants without an assigned speaking side go left.
- Only group messages gain coloured initial avatars. Immediately consecutive ordinary messages from the same lane hide the repeated visible name/avatar while preserving the avatar gutter and accessible speaker identity. A different speaker, thought, action, notice or divider breaks the run; recipients changing does not. Every event remains independently seekable with its type, recipients and payload control intact.
- Messages show recorded `to_agent`/`to_agents` recipients in both dyad and group modes. Non-negative integer seats map through the original roster order, with `agent_N` for missing identities; malformed seats are ignored and repeated seats are deduplicated in recorded order (the single field first when both fields exist). `all` is only an abbreviation for recorded seats exactly covering a complete, unambiguous roster's non-sender seats with a known sender. Missing recipients or a channel/event type alone never imply `all`. These labels describe recorded routing, not visibility; initials and names are presentation rather than new durable identities.
- Negotiation's discovered `agent_a`/`agent_b` lanes are not aliases for differently named roster seats. Its `agent`-tagged thoughts and other actions belong to the same discovered lanes as its speech, in both Lanes and chat.
- Participant-owned, nonempty text with event type `thinking` or `reasoning` renders as a dashed, untinted thought envelope, collapsed to type, participant and a one-line preview. System variants remain notices, and silent variants remain actions/dividers. The allow-list identifies event types, not public/private visibility; other text events carrying `agent` remain messages.
- Speaking lanes are those carrying ordinary messages, not thoughts. A thought follows its lane's side even before that lane's first message; thought/action-only lanes default left without inventing seat aliases.
- Chat colours derive from the same `laneHue` as Lanes, with chat-only light/dark tint tokens; neither view claims public/private visibility.
- Clicking a row, or pressing Enter/Space on its focused list item, seeks the shared cursor and pauses playback. The active row scrolls into view and future rows dim. Native thought and `payload` disclosures are separate controls, so payload stays reachable when a thought is collapsed. Mouse/keyboard interaction with either disclosure or its contents never seeks or pauses playback, and cursor changes preserve their open state.
- The default-disabled `Hide system events` filter hides only events with top-level `synthetic === true`. The durable Python `Event.synthetic: bool = False` marker is optional boolean/null in TypeScript and propagates through the transcript. Missing, false, null and malformed raw flags remain visible; `data.synthetic` is ignored. There is no event-type fallback, prefix inference or migration: historical unmarked events remain visible, including formerly listed wrapper types. Filtering only hides existing rows, preserving original grouping boundaries, cursors, playback and native disclosure state.
- Producers explicitly mark Calendar `turn_start`/`turn_end` and `decide_start`/`decide_end`, including emitted retry wrappers, in `workflow_steps.py`; Negotiation marks `llm_streaming` and derived `project_auto_allocation` at emission in `engine.py`. Thinking/reasoning, chosen actions, submissions, `cell_applied` and phase/round/game semantics remain unmarked. Adapters forward envelope metadata, never infer it or put it in payloads. `EventLog.append` already accepts `**extra` for envelope fields.
- Negotiation `_emit(event_type, data, *, synthetic=False)` uses explicit optional keyword metadata. `on_event(callback, envelope_metadata=True)` opts durable consumers into `callback(event_type, data, synthetic=...)`; the default preserves existing two-argument callbacks without catching callback errors or inspecting signatures. The game adapter and legacy HTTP recorder opt in; existing observer/test callbacks deliberately keep their two-argument contract.
- Dotted `turn.started`/`turn.finished`, `cell.*` and `llm.*` instrumentation has no current emitters in this checkout. Future producers must explicitly set top-level `synthetic=True` for synthetic instrumentation; these names alone never hide anything. No emitters or classification tables are invented for them.

## The standalone replay cursor contract

An environment's replay page is a plain static page served from its own
directory. Its optional embedding capability can receive a cursor by `postMessage`; the Episode screen no longer sends these messages:

```javascript
window.addEventListener("message", event => {
  if (event.data?.type === "cursor") seek(event.data.index);
});
```

`index` is an absolute 1-based event position, so `seek` must be able to move
backwards. A renderer that accumulates into the DOM rebuilds from the start;
that is cheap at episode scale and is the only honest answer to an absolute
cursor.

The retained replay capability accepts `?embed=1&episode=<episode_uid>` and is expected to:

- hide its own chrome under `.embedded` — with CSS, because the `hidden`
  attribute loses to a `display` rule and an embed then silently keeps its
  toolbar;
- read the episode from `GET /api/episodes/<episode_uid>`, whose events live at
  `payload.episode.events`.

There is no web registration step. Some standalone pages take a Redis stream id rather than a persisted episode; their contracts remain environment-owned.

Two routes serve these pages, each confined to its own root:

| Route | Serves |
|---|---|
| `/environment-replays/<environment>/` | `games/<environment>/replay/` |
| `/environment-assets/<environment>/…` | anything else under `games/<environment>/` |

Both resolve and then check containment, so `..` returns 403 rather than a file.

## Verify

```bash
cd web && npm test && npm run build
docker compose up --build
# http://localhost:8080/episodes/ -> open one episode
```
