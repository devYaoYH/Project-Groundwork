import assert from "node:assert/strict";
import test from "node:test";

import { jsonPreview, jsonTree } from "./json-tree.ts";

test("normalizes nested objects and arrays without losing their order", () => {
  assert.deepEqual(jsonTree({ title: "study", levels: [1, { enabled: true }] }), {
    kind: "object",
    entries: [
      { key: "title", value: { kind: "scalar", value: '"study"' } },
      { key: "levels", value: {
        kind: "array",
        entries: [
          { kind: "scalar", value: "1" },
          { kind: "object", entries: [{ key: "enabled", value: { kind: "scalar", value: "true" } }] },
        ],
      } },
    ],
  });
});

test("retains empty containers and JSON scalar values", () => {
  assert.deepEqual(jsonTree({ none: null, off: false, emptyObject: {}, emptyArray: [] }), {
    kind: "object",
    entries: [
      { key: "none", value: { kind: "scalar", value: "null" } },
      { key: "off", value: { kind: "scalar", value: "false" } },
      { key: "emptyObject", value: { kind: "object", entries: [] } },
      { key: "emptyArray", value: { kind: "array", entries: [] } },
    ],
  });
});

test("keeps long and whitespace-bearing strings readable and quoted", () => {
  const value = "A long value\nthat retains whitespace and is not truncated.";
  const node = jsonTree(value);
  assert.equal(node.kind, "scalar");
  assert.equal(node.value, JSON.stringify(value));
  assert.equal(jsonPreview({ one: 1 }), "object (1)");
  assert.equal(jsonPreview([]), "array (0)");
});
