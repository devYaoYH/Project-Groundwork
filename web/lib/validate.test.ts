import assert from "node:assert/strict";
import test from "node:test";

import type { EnvironmentDetail } from "./api.ts";
import { parseClientDesign, validateClientDesign } from "./validate.ts";

const detail: EnvironmentDetail = {
  environment_id: "test",
  release: { release_id: "test_release", version: "v1", declaration_sha256: "x", item_bank_sha256: "y", oracle_version: "v1" },
  parameters: [
    { name: "item", type: "categorical", domain: ["a", "b"], fixed: false, source: "item", item_key: null, levels: [{ value: "a", count: 1 }, { value: "b", count: 1 }], identity_grained: false },
    { name: "mode", type: "categorical", domain: ["on", "off"], fixed: false, source: "design", item_key: null, levels: null, identity_grained: false },
  ],
  roles: [{ id: "player", count: 1, accepts: ["scripted"], description: "" }],
  measures: [],
  item_policy: { mode: "enumerate", bank_path: "items.jsonl", item_bank_sha256: "y", item_count: 2 },
  declaration_yaml: "",
};

test("mirrors missing-disposition and randomize-on-design checks", () => {
  const design = parseClientDesign([
    "release: test@v1",
    "parameters:",
    "  mode: {randomize: true}",
    "units: {episodes_per_cell: 1}",
    "roster: [{id: p, role: player, kind: scripted, binding: baseline}]",
    "seed: {root: 1}",
  ].join("\n"));
  const messages = validateClientDesign(design, detail).map((issue) => issue.message).join(" ");
  assert.match(messages, /Missing disposition/);
  assert.match(messages, /design-sourced/);
});

test("rejects unknown item levels without server I/O", () => {
  const design = parseClientDesign([
    "release: test@v1",
    "parameters:",
    "  item: {pin: missing}",
    "units: {episodes_per_cell: 1}",
    "roster: [{id: p, role: player, kind: scripted, binding: baseline}]",
    "seed: {root: 1}",
  ].join("\n"));
  assert.match(validateClientDesign(design, detail)[0].message, /no stratum/);
});

// Every shape below is something a design passes through while being typed.
// None of them may throw: the editor validates on a pause, not on completion.
test("a parameter key typed without a body reports instead of throwing", () => {
  const design = parseClientDesign([
    "release: test@v1",
    "parameters:",
    "  mode:",
  ].join("\n"));
  const issues = validateClientDesign(design, detail);
  assert.match(issues.map((issue) => issue.message).join(" "), /Expected \{factor/);
  assert.ok(issues.every((issue) => typeof issue.path === "string"));
});

test("a scalar or list where a disposition belongs reports instead of throwing", () => {
  for (const body of ["  mode: 5", "  mode: [1, 2]"]) {
    const design = parseClientDesign(["release: test@v1", "parameters:", body].join("\n"));
    assert.match(
      validateClientDesign(design, detail).map((issue) => issue.message).join(" "),
      /Expected \{factor/,
    );
  }
});

test("a non-list factor reports instead of throwing on iteration", () => {
  const design = parseClientDesign([
    "release: test@v1",
    "parameters:",
    "  mode: {factor: on}",
  ].join("\n"));
  assert.match(
    validateClientDesign(design, detail).map((issue) => issue.message).join(" "),
    /factor takes a list/,
  );
});

test("a half-written roster entry reports instead of throwing", () => {
  const design = parseClientDesign([
    "release: test@v1",
    "parameters:",
    "  mode: {pin: on}",
    "roster:",
    "  - id: p",
    "  -",
  ].join("\n"));
  const messages = validateClientDesign(design, detail).map((issue) => issue.message).join(" ");
  assert.match(messages, /Each participant needs/);
  // Arity counts what was written, so the stray entry is still two participants.
  assert.match(messages, /requires 1 participants/);
});

test("a roster that is not a list yet reports instead of throwing", () => {
  const design = parseClientDesign(["release: test@v1", "roster: 3"].join("\n"));
  assert.match(
    validateClientDesign(design, detail).map((issue) => issue.message).join(" "),
    /roster takes a list/,
  );
});

test("typing the word_guess design one character at a time never throws", () => {
  const full = [
    "release: test@v1",
    "parameters:",
    "  item: {factor: [a, b]}",
    "  mode: {pin: on}",
    "units: {episodes_per_cell: 2}",
    "roster: [{id: p, role: player, kind: scripted, binding: baseline}]",
    "seed: {root: 1}",
  ].join("\n");
  for (let index = 0; index <= full.length; index += 1) {
    const prefix = full.slice(0, index);
    assert.doesNotThrow(
      () => validateClientDesign(parseClientDesign(prefix), detail),
      `threw on prefix of length ${index}:\n${prefix}`,
    );
  }
});
