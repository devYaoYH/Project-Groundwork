import assert from "node:assert/strict";
import test from "node:test";
import { projectChat } from "./chat.ts";

test("private images precede their recipient's response without entering peer turns", () => {
  const image = { type: "image", asset: `asset://${"a".repeat(64)}`, sha256: "a".repeat(64), mime_type: "image/png" };
  const result = projectChat([
    { type: "round_start", data: { round: 1 } },
    { type: "observation", data: { speaker: "director", content: [{ type: "text", text: "Your board" }, image], visibility: "private" } },
    { type: "message", data: { speaker: "director", text: "The tall figure first" } },
    { type: "action", data: { speaker: "director", done: true } },
    { type: "observation", data: { speaker: "matcher", content: "Your gallery", visibility: "private" } },
    { type: "message", data: { speaker: "matcher", text: "I see it" } },
    { type: "round_end", data: { round: 1 } },
  ]);
  assert.equal(result.length, 4);
  assert.equal(result[1].inputText, "Your board");
  assert.equal(result[1].cursor, 1);
  assert.equal(result[1].endCursor, 3);
  assert.deepEqual(result[1].messages.map(message => message.text), ["The tall figure first"]);
  assert.equal(result[2].input, "Your gallery");
  assert.equal(JSON.stringify(result[2]).includes(image.sha256), false);
});

test("legacy messages remain chat turns without invented observations", () => {
  const turns = projectChat([
    { type: "message", data: { speaker: "a", text: "hello" } },
    { type: "message", data: { speaker: "b", text: "yes" } },
  ]);
  assert.deepEqual(turns.map(turn => [turn.laneId, turn.input]), [["a", null], ["b", null]]);
});

test("silent input, malformed payloads and private responses remain distinguishable", () => {
  assert.deepEqual(projectChat(null), []);
  const turns = projectChat([null, {},
    { type: "observation", data: { speaker: "a", content: [{ type: "image", sha256: "private" }] } },
    { type: "private_report", data: { speaker: "a", text: "I prefer B", visibility: "private" } },
  ]);
  assert.equal(turns.length, 1);
  assert.equal(turns[0].inputText, null);
  assert.equal(turns[0].messages[0].private, true);
});
