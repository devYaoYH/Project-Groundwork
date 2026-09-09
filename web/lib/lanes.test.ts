import assert from "node:assert/strict";
import test from "node:test";

import type { Lane, TraceEvent } from "./api.ts";
import { SYSTEM_LANE, projectLanes, projectTranscript } from "./lanes.ts";

const roster: Lane[] = [
  { participant_id: "agent_a", kind: "llm", binding: "gpt-4o-mini" },
  { participant_id: "agent_b", kind: "scripted", binding: "baseline" },
];

function marksOf(rows: ReturnType<typeof projectLanes>["rows"], id: string) {
  return rows.find((row) => row.participant_id === id)?.marks ?? [];
}

test("assigns a lane from speaker, from participant_id, and from neither", () => {
  const events: TraceEvent[] = [
    { type: "cheap_talk", data: { speaker: "agent_a", text: "opening offer" } },
    { type: "decision", data: { participant_id: "agent_b", choice: 3 } },
    { type: "episode_start", data: { num_agents: 2 } },
  ];

  const { rows, cursorMax } = projectLanes(events, roster, null);

  assert.equal(cursorMax, 3);
  assert.deepEqual(marksOf(rows, "agent_a").map((mark) => mark.cursor), [0]);
  assert.deepEqual(marksOf(rows, "agent_b").map((mark) => mark.cursor), [1]);
  assert.deepEqual(marksOf(rows, SYSTEM_LANE).map((mark) => mark.cursor), [2]);
  // The system lane is a catch-all, so it sorts after the pinned participants.
  assert.equal(rows[rows.length - 1].participant_id, SYSTEM_LANE);
});

test("an utterance is the event carrying text; everything else is a committed action", () => {
  const events: TraceEvent[] = [
    { type: "cheap_talk", data: { speaker: "agent_a", text: "let us split it" } },
    { type: "decision", data: { speaker: "agent_a", allocation: [4, 2] } },
  ];

  const shapes = marksOf(projectLanes(events, roster, null).rows, "agent_a").map((mark) => mark.shape);
  assert.deepEqual(shapes, ["utterance", "block"]);
});

test("Calendar numeric agent ids resolve through the pinned roster", () => {
  const events: TraceEvent[] = [
    { type: "turn_start", data: { agent_id: 0 } },
    { type: "turn_end", data: { agent_id: 1, text: "I can make that time." } },
    { type: "resolution", data: { agent_id: null } },
  ];

  const { rows } = projectLanes(events, roster, "round");

  assert.deepEqual(marksOf(rows, "agent_a").map((mark) => mark.cursor), [0]);
  assert.deepEqual(marksOf(rows, "agent_b").map((mark) => mark.cursor), [1]);
  assert.deepEqual(marksOf(rows, SYSTEM_LANE).map((mark) => mark.cursor), [2]);
});

test("an unpinned Calendar seat remains a discoverable legacy lane", () => {
  const event: TraceEvent = { type: "turn_end", data: { agent_id: 3 } };
  assert.equal(projectLanes([event], null, null).rows[0].participant_id, "agent_3");
});

test("Calendar chat content is an utterance when text is absent", () => {
  const event: TraceEvent = { type: "dm_sent", data: { agent_id: 0, content: "hello" } };
  const projection = projectLanes([event], roster, null);
  assert.deepEqual(marksOf(projection.rows, "agent_a").map((mark) => [mark.shape, mark.text]), [["utterance", "hello"]]);
  assert.deepEqual(projectTranscript([event], roster)[0], {
    cursor: 0,
    laneId: "agent_a",
    type: "dm_sent",
    text: "hello",
    data: { agent_id: 0, content: "hello" },
  });
});

test("a sequence-grained environment gets separators only where the index is present", () => {
  const events: TraceEvent[] = [
    { type: "episode_start", data: {} },                                  // no index at all
    { type: "phase_start", data: { round: 1, phase: "cheap_talk" } },     // round 1 begins
    { type: "cheap_talk", data: { speaker: "agent_a", text: "hi", round: 1 } },
    { type: "thinking", data: { speaker: "agent_b" } },                   // index absent again
    { type: "phase_start", data: { round: 2, phase: "decision" } },       // round 2 begins
    { type: "decision", data: { speaker: "agent_b", round: 2 } },
  ];

  const { separators } = projectLanes(events, roster, "round");

  assert.deepEqual(separators, [
    { cursor: 1, label: "round", value: 1 },
    { cursor: 4, label: "round", value: 2 },
  ]);
});

test("an episode-grained environment gets a continuous lane and no separators", () => {
  // Word-Guess supplies no index. Absence is merely absent, so the projection
  // draws nothing rather than inventing a boundary per event.
  const events: TraceEvent[] = [
    { type: "guess", data: { speaker: "guesser", text: "kite" } },
    { type: "hint", data: { speaker: "hinter", text: "it flies" } },
  ];

  const { separators, rows } = projectLanes(events, roster, null);

  assert.deepEqual(separators, []);
  assert.equal(rows.filter((row) => row.marks.length > 0).length, 2);
});

test("Word Guess role speakers map to the uniquely declared participants", () => {
  const roleRoster: Lane[] = [
    { participant_id: "guesser_1", role: "guesser", kind: "scripted", binding: "baseline" },
    { participant_id: "host_1", role: "host", kind: "scripted", binding: "baseline" },
  ];
  const events: TraceEvent[] = [
    { type: "game_start", data: { max_turns: 6 } },
    { type: "message", data: { speaker: "guesser", text: "is it an animal?" } },
    { type: "message", data: { speaker: "host", text: "yes" } },
    { type: "game_end", data: { won: true } },
  ];

  const projection = projectLanes(events, roleRoster, null);

  assert.deepEqual(marksOf(projection.rows, "guesser_1").map((mark) => mark.cursor), [1]);
  assert.deepEqual(marksOf(projection.rows, "host_1").map((mark) => mark.cursor), [2]);
  assert.deepEqual(marksOf(projection.rows, SYSTEM_LANE).map((mark) => mark.cursor), [0, 3]);
});

test("ambiguous and unknown role speakers remain discovered lanes", () => {
  const ambiguous: Lane[] = [
    { participant_id: "guesser_1", role: "guesser", kind: "scripted", binding: "one" },
    { participant_id: "guesser_2", role: "guesser", kind: "scripted", binding: "two" },
  ];
  const events: TraceEvent[] = [
    { type: "message", data: { speaker: "guesser", text: "hello" } },
    { type: "message", data: { speaker: "observer", text: "not a declared role" } },
  ];

  const projection = projectLanes(events, ambiguous, null);

  assert.equal(projection.rows.find((row) => row.participant_id === "guesser")?.declared, false);
  assert.equal(projection.rows.find((row) => row.participant_id === "observer")?.declared, false);
});

test("an index the events carry is ignored unless the release declares one", () => {
  const events: TraceEvent[] = [{ type: "phase_start", data: { round: 1 } }];
  assert.deepEqual(projectLanes(events, roster, null).separators, []);
});

// Everything below is a shape a real episode arrives in. None of them may throw.
test("an episode with no events projects empty lanes rather than failing", () => {
  const projection = projectLanes([], roster, "round");
  assert.equal(projection.cursorMax, 0);
  assert.deepEqual(projection.separators, []);
  assert.deepEqual(projection.rows.map((row) => row.participant_id), ["agent_a", "agent_b"]);
});

test("a trace with no roster still shows the speakers its events name", () => {
  const events: TraceEvent[] = [{ type: "message", data: { speaker: "agent_a", text: "hello" } }];
  for (const lanes of [null, undefined, []]) {
    const { rows } = projectLanes(events, lanes, null);
    assert.deepEqual(rows.map((row) => row.participant_id), ["agent_a"]);
    assert.equal(rows[0].declared, false);
  }
});

test("null, missing and malformed payloads are tolerated", () => {
  const events = [
    null,
    undefined,
    {},
    { type: "message" },
    { type: "message", data: null },
    { type: "message", data: ["not", "a", "dict"] as unknown as Record<string, unknown> },
    { type: null, data: { speaker: 7, text: 12 } },
  ] as (TraceEvent | null | undefined)[];

  const { rows, cursorMax } = projectLanes(events, roster, "round");
  assert.equal(cursorMax, 7);
  // A numeric speaker is still an identity; only an absent one falls to system.
  assert.deepEqual(marksOf(rows, "7").map((mark) => mark.text), ["12"]);
  assert.equal(marksOf(rows, SYSTEM_LANE).length, 6);
  assert.deepEqual(projectLanes(null, null, null), { rows: [], separators: [], cursorMax: 0 });
});

test("the transcript keeps one line per event, including the silent ones", () => {
  const events: TraceEvent[] = [
    { type: "message", data: { speaker: "agent_a", text: "hello" } },
    { type: "episode_end", data: {} },
  ];
  assert.deepEqual(projectTranscript(events), [
    {
      cursor: 0, laneId: "agent_a", type: "message", text: "hello",
      data: { speaker: "agent_a", text: "hello" },
    },
    { cursor: 1, laneId: SYSTEM_LANE, type: "episode_end", text: null, data: {} },
  ]);
  assert.deepEqual(projectTranscript(undefined), []);
});
