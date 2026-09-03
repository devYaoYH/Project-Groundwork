# Episode viewers

Two viewers coexist, deliberately.

The **standard lane view** is what every environment gets out of the box, with
no front-end work: one lane per participant, a scrubber, a transcript, and the
generating execution config disclosed. A new environment is inspectable on the
day it first runs.

A **specialised replay viewer** is optional. An environment that benefits from a
bespoke rendering ships one under `games/<environment>/replay/`, and it mounts
alongside the standard view driven by the same cursor.

The per-event renderer registry the retired hand-rolled viewer carried is gone. It
solved a smaller problem — swapping the DOM for one event type — at the cost of
a shared entry point every environment had to edit. The two mechanisms below
replace it.

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
| `event.data.speaker`, else `event.data.participant_id` | which lane the event belongs to; neither means the `system` lane |
| `event.data.text` | the event reads as an utterance rather than a committed action, and appears in the transcript |
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

## The specialised-viewer cursor contract

An environment's replay page is a plain static page served from its own
directory. It receives the cursor by `postMessage` and nothing else:

```javascript
window.addEventListener("message", event => {
  if (event.data?.type === "cursor") seek(event.data.index);
});
```

`index` is an absolute 1-based event position, so `seek` must be able to move
backwards. A renderer that accumulates into the DOM rebuilds from the start;
that is cheap at episode scale and is the only honest answer to an absolute
cursor.

The page is mounted with `?embed=1&episode=<episode_uid>` and is expected to:

- hide its own chrome under `.embedded` — with CSS, because the `hidden`
  attribute loses to a `display` rule and an embed then silently keeps its
  toolbar;
- read the episode from `GET /api/episodes/<episode_uid>`, whose events live at
  `payload.episode.events`.

Register the page in `web/lib/environments.ts` (`specialisedViewers`) to have it
mounted beside the lane view. Membership is deliberately narrow: a page that
takes a Redis stream id rather than an episode would render an empty panel and
claim the environment ships a viewer it does not.

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
