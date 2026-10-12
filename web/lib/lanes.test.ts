import assert from "node:assert/strict";
import test from "node:test";

import type { Lane, TraceEvent } from "./api.ts";
import { SYSTEM_LANE, keepWhenHidingSystemEvents, laneOf, projectChat, projectLanes, projectTranscript } from "./lanes.ts";

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

function chatOf(events: Parameters<typeof projectTranscript>[0], lanes: Parameters<typeof projectLanes>[1] = null) {
  return projectChat(projectTranscript(events, lanes), projectLanes(events, lanes, null).rows, lanes);
}

test("Hide system events retains semantic and unknown events regardless of round", () => {
  const events: TraceEvent[] = [
    { type: "message", data: { speaker: "agent_a", text: "Hello" } },
    { type: "notice", data: { text: "Submit allocations" } },
    { type: "phase_start", data: { round: 0 } },
    { type: "resolution", data: { round: "final" } },
    { type: "game_end", data: { round: 1 } },
    { type: "thinking", data: { agent: "agent_a", content: "Thought", round: 1 } },
    { type: "reasoning", data: { agent: "agent_b", text: "Reason" } },
    { type: "turn_start", synthetic: true, data: { agent_id: 0, round: 1 } },
    { type: "llm_streaming", synthetic: true, data: {} },
    { type: "episode_start", data: {} },
    { type: "game_complete", data: { rounds: 1 } },
    ...[null, "", "  ", true, {}, [], NaN, Infinity, -Infinity].map((round) => ({ type: "round_start", data: { round } })),
  ];
  const chat = chatOf(events, roster);
  assert.deepEqual(chat.rows.filter(keepWhenHidingSystemEvents).map((row) => row.cursor), events.map((_, cursor) => cursor).filter((cursor) => cursor !== 7 && cursor !== 8));
  assert.deepEqual(chat.rows.map((row) => row.cursor), events.map((_, cursor) => cursor));
});

test("Hide system events preserves grouping breaks at hidden original boundaries", () => {
  const message = { type: "message", data: { speaker: "alice", text: "Hello" } };
  const chat = chatOf([message, message,
    { type: "llm_streaming", synthetic: true, data: {} }, message,
    { type: "thinking", data: { agent: "alice", content: "Thought" } }, message,
    { type: "turn_start", synthetic: true, data: { agent_id: 0 } }, message,
  ], calendarRoster);
  assert.deepEqual(chat.rows.filter(keepWhenHidingSystemEvents).map((row) => [row.cursor, row.continued]),
    [[0, false], [1, true], [3, false], [4, false], [5, false], [7, false]]);
});

test("Hide system events uses only literal top-level true, never types or payload flags", () => {
  const hidden = ["turn.started", "turn.finished", "cell.started", "cell.finished", "llm.attempt", "llm.request", "llm.response", "llm_streaming", "turn_start", "turn_end", "decide_start", "decide_end", "project_auto_allocation"];
  const visible = ["thinking", "reasoning", "phase_start", "round_start", "round_complete", "game_start", "game_end", "game_complete", "early_decision", "decision", "submitted", "cell_applied", "cell_rejected", "cell_rolled_back", "cell_finished", "cell.future", "llm.future", "turn.future", "unknown_action"];
  for (const type of [...hidden, ...visible]) {
    for (const data of [{}, { agent: "agent_a", allocation: [1, 0, 1] }, { speaker: "system", text: "Recorded text", round: 0 }]) {
      for (const synthetic of [undefined, null, false, true, "true", "false", 1, 0, {}, []]) {
        const event = { type, data: { ...data, synthetic: true }, synthetic } as unknown as TraceEvent;
        const row = chatOf([event], roster).rows[0];
        assert.equal(keepWhenHidingSystemEvents(row), synthetic !== true, type);
        assert.equal(row.synthetic, synthetic);
        assert.equal(row.data, event.data);
        assert.equal(row.cursor, 0);
      }
      assert.equal(keepWhenHidingSystemEvents(chatOf([{ type, data }]).rows[0]), true);
    }
  }
});

for (const roles of [["seller", "buyer"], ["guesser", "host"]]) {
  test(`${roles.join(" / ")} chat uses mapped identities and Lanes order, not utterance order`, () => {
    const lanes = roles.map((role) => ({ participant_id: `${role}_seat`, role, kind: "scripted", binding: null }));
    const chat = chatOf([
      { type: "message", data: { speaker: roles[1], text: "I speak first" } },
      { type: "message", data: { speaker: roles[0], text: "I am the first lane" } },
      { type: "turn_end", data: { participant_id: lanes[1].participant_id } },
    ], lanes);
    assert.equal(chat.mode, "dyad");
    assert.deepEqual(chat.rows.map((row) => [row.laneId, row.kind, row.side]), [
      [lanes[1].participant_id, "message", "right"],
      [lanes[0].participant_id, "message", "left"],
      [lanes[1].participant_id, "action", "right"],
    ]);
  });
}

test("Negotiation discovered speakers and thoughts form a dyad beside two silent roster seats", () => {
  const lanes = ["first", "second"].map((participant_id) => ({ participant_id, role: "negotiator", kind: "llm", binding: null }));
  const events = [
    { type: "thinking", data: { agent: "agent_a", content: "Books first" } },
    { type: "cheap_talk", data: { speaker: "agent_a", text: "Both books?" } },
    { type: "cheap_talk", data: { speaker: "agent_b", text: "One each." } },
  ];
  const chat = chatOf(events, lanes);
  assert.equal(chat.mode, "dyad");
  assert.deepEqual(chat.rows.map((row) => [row.laneId, row.kind, row.side]), [
    ["agent_a", "thought", "left"], ["agent_a", "message", "left"], ["agent_b", "message", "right"],
  ]);
});

test("hybrid group mode counts distinct valid roster ids or more than two speaking lanes", () => {
  const events = ["alice", "bob"].map((speaker) => ({ data: { speaker, text: "hello" } }));
  const lanes = ["alice", "bob", "carol"].map((participant_id) => ({ participant_id, kind: "llm", binding: null }));
  const rosterGroup = chatOf(events, lanes);
  assert.equal(rosterGroup.mode, "group");
  assert.deepEqual(rosterGroup.rows.map((row) => row.side), ["left", "left"]);
  const discoveredGroup = chatOf([...events, { data: { speaker: "carol", text: "hi" } }]);
  assert.equal(discoveredGroup.mode, "group");
  assert.deepEqual(discoveredGroup.rows.map((row) => row.side), ["left", "left", "left"]);
  const malformedRoster = [lanes[0], lanes[1], lanes[0], null, undefined,
    { participant_id: "", kind: "llm", binding: null },
    { participant_id: 7, kind: "llm", binding: null } as unknown as Lane,
    { participant_id: "system", kind: "unknown", binding: null }];
  assert.equal(chatOf(events, malformedRoster).mode, "dyad");
});

test("all four chat kinds preserve every cursor, text and original data object", () => {
  const events = [
    { type: "game_start", data: { round: 1 } },
    { type: "notice", data: { speaker: "system", text: "Submit allocations" } },
    { type: "turn_start", data: { speaker: "agent_b", text: "", content: "Do not fall back from empty text" } },
    { type: "message", data: { speaker: "agent_a", content: "legacy text", to_agent: 1 } },
    { type: "cheap_talk", data: { speaker: "agent_b", text: "An ordinary message" } },
    { type: "turn_start", data: { speaker: "observer" } },
  ];
  const lines = projectTranscript(events, roster);
  const chat = projectChat(lines, projectLanes(events, roster, null).rows, roster);
  assert.deepEqual(chat.rows.map((row) => [row.kind, row.side]), [
    ["divider", "center"], ["notice", "center"], ["action", "right"],
    ["message", "left"], ["message", "right"], ["action", "left"],
  ]);
  chat.rows.forEach((row, cursor) => {
    const { kind, side, continued, recipients, ...original } = row;
    assert.deepEqual(original, lines[cursor]);
    assert.equal(row.data, events[cursor].data);
  });
  assert.equal(chat.rows[2].text, "");
});

test("empty, single-speaker, system-only and malformed chat input stay addressable", () => {
  assert.deepEqual(projectChat(undefined, undefined), { mode: "dyad", rows: [] });
  assert.deepEqual(chatOf([]), { mode: "dyad", rows: [] });
  const single = chatOf([{ data: { speaker: "agent_b", text: "Only speaker" } }], roster);
  assert.equal(single.rows[0].side, "left");
  const events = [null, undefined, {}, { data: null },
    { data: ["bad"] as unknown as Record<string, unknown> },
    { data: { text: "" } }, { data: { text: false } },
    { data: { text: { invalid: true }, content: 12 } }];
  const chat = chatOf(events);
  assert.equal(chat.mode, "dyad");
  assert.deepEqual(chat.rows.map((row) => row.cursor), events.map((_, index) => index));
  assert.deepEqual(chat.rows.map((row) => row.kind), ["divider", "divider", "divider", "divider", "divider", "divider", "notice", "notice"]);
  assert.ok(chat.rows.every((row) => row.side === "center"));
  assert.deepEqual(chat.rows.slice(-2).map((row) => row.text), ["false", "12"]);
});

test("agent attribution uses exact ids, unique roles and discovered names in both projections", () => {
  const lanes: Lane[] = [
    { participant_id: "first", role: "seller", kind: "llm", binding: null },
    { participant_id: "second", role: "buyer", kind: "llm", binding: null },
    { participant_id: "seller", role: "observer", kind: "llm", binding: null },
  ];
  const events: TraceEvent[] = [
    { type: "thinking", data: { agent: "first", content: "Exact id" } },
    { type: "reasoning", data: { agent: "buyer", content: "Unique role" } },
    { type: "llm_streaming", data: { agent: "agent_a" } },
    { type: "thinking", data: { agent: "seller", content: "Id wins over role" } },
  ];
  const expected = ["first", "second", "agent_a", "seller"];
  assert.deepEqual(events.map((event) => laneOf(event, lanes)), expected);
  const projection = projectLanes(events, lanes, null);
  const lines = projectTranscript(events, lanes);
  const chat = projectChat(lines, projection.rows, lanes);
  assert.deepEqual(chat.rows.map((row) => row.laneId), expected);
  assert.deepEqual(chat.rows.map((row) => row.kind), ["thought", "thought", "action", "thought"]);
  chat.rows.forEach((row, cursor) => {
    const mark = marksOf(projection.rows, row.laneId).find((mark) => mark.cursor === cursor)!;
    assert.equal(mark.text, row.text);
    assert.equal(row.data, events[cursor].data);
    const { kind, side, continued, recipients, ...original } = row;
    assert.deepEqual(original, lines[cursor]);
  });
  assert.equal(projection.rows.find((row) => row.participant_id === "agent_a")?.declared, false);
  const ambiguous = lanes.map((lane) => ({ ...lane, role: "negotiator" }));
  assert.equal(laneOf({ data: { agent: "negotiator" } }, ambiguous), "negotiator");
});

test("speaker and participant_id precede agent, and named attribution precedes numeric seats", () => {
  const cases: [Record<string, unknown>, string][] = [
    [{ speaker: "agent_a", participant_id: "agent_b", agent: "other", agent_id: 1 }, "agent_a"],
    [{ participant_id: "agent_b", agent: "other", agent_id: 0 }, "agent_b"],
    [{ speaker: "system", agent: "agent_a", agent_id: 0 }, "system"],
    [{ agent: "discovered", agent_id: 0 }, "discovered"],
    [{ speaker: null, participant_id: {}, agent: "agent_b", agent_id: 0 }, "agent_b"],
    [{ agent: {}, agent_id: 0 }, "agent_a"],
    [{ agent: null, agent_id: 1 }, "agent_b"],
    [{ agent_id: 3 }, "agent_3"],
    [{ agent_id: -1 }, "system"],
    [{ agent_id: 0.5 }, "system"],
    [{ agent_id: "0" }, "system"],
    [{}, "system"],
  ];
  for (const [data, expected] of cases) {
    assert.equal(laneOf({ data }, roster), expected, JSON.stringify(data));
  }
});

test("only participant-owned nonempty thinking/reasoning text becomes a thought", () => {
  const events: TraceEvent[] = [
    { type: "thinking", data: { agent: "agent_a", content: "Thought" } },
    { type: "reasoning", data: { participant_id: "agent_b", text: "Reason" } },
    { type: "cheap_talk", data: { agent: "agent_b", text: "Not a thought" } },
    { type: "reflection", data: { agent: "agent_a", content: "Not allow-listed" } },
    { type: "thinking", data: { speaker: "system", agent: "agent_a", text: "System notice" } },
    { type: "reasoning", data: { content: "Unattributed notice" } },
    { type: "thinking", data: { agent: "agent_b", text: "", content: "Empty text wins" } },
    { type: "reasoning", data: { agent: "agent_a" } },
    { type: "thinking", data: {} },
  ];
  const chat = chatOf(events, roster);
  assert.deepEqual(chat.rows.map((row) => [row.kind, row.side]), [
    ["thought", "left"], ["thought", "right"], ["message", "right"], ["message", "left"],
    ["notice", "center"], ["notice", "center"], ["action", "right"], ["action", "left"], ["divider", "center"],
  ]);
  assert.deepEqual(chat.rows.map((row) => row.cursor), events.map((_, cursor) => cursor));
});

test("thoughts preceding messages use speaking Lanes order; thought/action-only lanes default left", () => {
  const events: TraceEvent[] = [
    { type: "thinking", data: { agent: "agent_b", content: "Before B speaks" } },
    { type: "reasoning", data: { agent: "agent_a", content: "Before A speaks" } },
    { type: "thinking", data: { agent: "observer", content: "Only a thought" } },
    { type: "llm_streaming", data: { agent: "silent" } },
    { type: "cheap_talk", data: { speaker: "agent_b", text: "B speaks first" } },
    { type: "cheap_talk", data: { speaker: "agent_a", text: "A follows" } },
  ];
  const chat = chatOf(events, roster);
  assert.equal(chat.mode, "dyad");
  assert.deepEqual(chat.rows.map((row) => row.side), ["right", "left", "left", "left", "right", "left"]);
  const thoughtsOnly = chatOf(events.slice(0, 4), roster);
  assert.equal(thoughtsOnly.mode, "dyad");
  assert.ok(thoughtsOnly.rows.every((row) => row.side === "left"));
  const oneSpeaker = chatOf(events.slice(0, 5), roster);
  assert.equal(oneSpeaker.mode, "dyad");
  assert.ok(oneSpeaker.rows.every((row) => row.side === "left"));
  const noRoster = chatOf(events);
  assert.equal(noRoster.mode, "dyad");
  assert.deepEqual(noRoster.rows.map((row) => row.side), ["left", "right", "left", "left", "left", "right"]);
});

const calendarRoster: Lane[] = ["alice", "bob", "carol", "dan"].map((participant_id) => ({ participant_id, kind: "llm", binding: null }));

test("group continuation uses only the immediately preceding ordinary message, regardless of recipients", () => {
  const message = (agent_id: number, to_agent: number): TraceEvent => ({ type: "dm_sent", data: { agent_id, to_agent, content: "hello" } });
  const boundaries: TraceEvent[] = [
    message(1, 2),
    { type: "thinking", data: { agent_id: 0, content: "thought" } },
    { type: "reasoning", data: { agent_id: 0, content: "reason" } },
    { type: "turn_start", data: { agent_id: 0 } },
    { type: "notice", data: { text: "system notice" } },
    { type: "resolution", data: {} },
  ];
  const events = [message(0, 1), message(0, 2), ...boundaries.flatMap((boundary) => [boundary, message(0, 1), message(0, 2)])];
  const lines = projectTranscript(events, calendarRoster);
  const chat = chatOf(events, calendarRoster);
  assert.equal(chat.mode, "group");
  assert.equal(chat.rows.length, events.length);
  assert.deepEqual(chat.rows.map((row) => row.continued), [false, true, ...boundaries.flatMap(() => [false, false, true])]);
  chat.rows.forEach(({ kind, side, continued, recipients, ...original }, index) => {
    assert.deepEqual(original, lines[index]);
    assert.equal(original.data, events[index].data);
  });
  assert.deepEqual(chatOf(events.slice(0, 2), calendarRoster.slice(0, 2)).rows.map((row) => row.continued), [false, false]);
  const discovered = chatOf([...events.slice(0, 2), message(1, 0), message(2, 0)]);
  assert.equal(discovered.mode, "group");
  assert.deepEqual(discovered.rows.map((row) => row.continued), [false, true, false, false]);
});

test("recipient seats use original roster order, keep zero and deduplicate recorded seats", () => {
  const cases: [Record<string, unknown>, string[] | null][] = [
    [{ to_agent: 0 }, ["alice"]],
    [{ to_agent: 2 }, ["carol"]],
    [{ to_agents: [2, 0, 2, 1, 0] }, ["carol", "alice", "bob"]],
    [{ to_agent: 2, to_agents: [1, 2, 0] }, ["carol", "bob", "alice"]],
    [{ to_agents: [] }, null], [{}, null],
    [{ to_agent: -1, to_agents: [null, undefined, -2, 0.5, "0", true, {}, [], NaN, Infinity] }, null],
    [{ to_agent: [0], to_agents: "1" }, null],
    [{ to_agent: "2", to_agents: [2, -1, 0.5, 0, false] }, ["carol", "alice"]],
  ];
  for (const [recipients, expected] of cases) {
    const chat = chatOf([{ type: "dm_sent", data: { agent_id: 1, content: "hi", ...recipients } }], calendarRoster);
    assert.deepEqual(chat.rows[0].recipients, expected);
  }
  // The sorted Lanes rows put system last; seat zero must not move with them.
  const originalOrder = [{ participant_id: "system", kind: "unknown", binding: null }, ...calendarRoster];
  assert.deepEqual(chatOf([{ data: { agent_id: 1, text: "hi", to_agents: [0, 2] } }], originalOrder).rows[0].recipients, ["system", "bob"]);
});

test("missing and malformed roster entries retain discoverable recipient seat names", () => {
  const lanes = [calendarRoster[0], null, undefined, { participant_id: "", kind: "llm", binding: null },
    { participant_id: 7, kind: "llm", binding: null } as unknown as Lane];
  const event = { data: { agent_id: 0, content: "hi", to_agents: [4, 1, 9, 2, 3, 1] } };
  assert.deepEqual(chatOf([event], lanes).rows[0].recipients, ["agent_4", "agent_1", "agent_9", "agent_2", "agent_3"]);
  for (const roster of [null, undefined, []]) {
    assert.deepEqual(chatOf([event], roster).rows[0].recipients, ["agent_4", "agent_1", "agent_9", "agent_2", "agent_3"]);
  }
});

test("all abbreviation requires exact known non-sender seats, never a channel or event guess", () => {
  const cases: [Record<string, unknown>, string[] | null][] = [
    [{ to_agents: [3, 1, 2, 1] }, ["all"]],
    [{ to_agents: [1, 2] }, ["bob", "carol"]],
    [{ to_agents: [0, 1, 2, 3] }, ["alice", "bob", "carol", "dan"]],
    [{ to_agents: [1, 2, 3, 4] }, ["bob", "carol", "dan", "agent_4"]],
    [{ to_agents: [0, 1, 2] }, ["alice", "bob", "carol"]],
    [{}, null],
  ];
  for (const [recipients, expected] of cases) {
    assert.deepEqual(chatOf([{ type: "all_groupchat_sent", data: { agent_id: 0, content: "hi", channel: "all_groupchat", ...recipients } }], calendarRoster).rows[0].recipients, expected);
  }
  const unknown = { data: { speaker: "unknown", text: "hi", to_agents: [1, 2, 3] } };
  assert.deepEqual(chatOf([unknown], calendarRoster).rows[0].recipients, ["bob", "carol", "dan"]);
  const ambiguous = [...calendarRoster.slice(0, 3), calendarRoster[2]];
  assert.deepEqual(chatOf([{ data: { agent_id: 0, text: "hi", to_agents: [1, 2, 3] } }], ambiguous).rows[0].recipients, ["bob", "carol", "carol"]);
  assert.deepEqual(chatOf([{ data: { agent_id: 0, text: "hi", to_agents: [1, 2] } }], [calendarRoster[0], calendarRoster[1], null]).rows[0].recipients, ["bob", "agent_2"]);
  assert.deepEqual(chatOf([{ data: { agent_id: 0, text: "hi", to_agent: 1 } }], calendarRoster.slice(0, 2)).rows[0].recipients, ["all"]);
  assert.deepEqual(chatOf([{ data: { agent_id: 0, text: "hi", to_agent: 0 } }], calendarRoster.slice(0, 1)).rows[0].recipients, ["alice"]);
});
