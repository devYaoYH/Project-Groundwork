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

test("calendar external bindings are optional while role acceptance still applies", () => {
  const calendar = { ...detail, environment_id: "calendar" };
  const design = parseClientDesign("release: calendar@v1\nparameters: {item: {pin: a}}\nroster: [{id: p, role: player, kind: scripted, runtime: external}]");
  assert.deepEqual(validateClientDesign(design, calendar), []);
  const wrongKind = parseClientDesign("release: calendar@v1\nparameters: {item: {pin: a}}\nroster: [{id: p, role: player, kind: llm, runtime: external}]");
  assert.ok(validateClientDesign(wrongKind, calendar).some((issue) => issue.path === "roster[0].kind"));
});

test("runtime-aware edits report malformed and unsupported options without throwing", () => {
  for (const value of ["human", "unknown", "42", "[local_process]"]) {
    const design = parseClientDesign(`release: test@v1\nroster: [{id: p, role: player, kind: scripted, runtime: ${value}}]`);
    assert.doesNotThrow(() => validateClientDesign(design, detail));
    assert.ok(validateClientDesign(design, detail).some((issue) => issue.path === "roster[0].runtime"));
  }
  for (const value of ["native_tools", "[scripted]", "42"]) {
    const design = parseClientDesign(`release: test@v1\nroster: [{id: p, role: player, kind: scripted, binding: baseline, harness: ${value}}]`);
    assert.ok(validateClientDesign(design, detail).some((issue) => issue.path === "roster[0].harness"));
  }
});

test("remote runtimes on other games and local scripted bindings are rejected", () => {
  const remote = parseClientDesign("release: test@v1\nroster: [{id: p, role: player, kind: scripted, binding: baseline, runtime: local_process}]");
  assert.ok(validateClientDesign(remote, detail).some((issue) => issue.path === "roster[0].runtime"));
  const local = parseClientDesign("release: calendar@v1\nroster: [{id: p, role: player, kind: scripted, binding: dsm, runtime: local_process}]");
  assert.ok(validateClientDesign(local, { ...detail, environment_id: "calendar" }).some((issue) => issue.path === "roster[0].binding"));
});

test("image role requires explicit verified capabilities while text stays compatible", () => {
  const design = { release: "test@v1", units: { episodes_per_cell: 1 }, seed: { root: 1 },
    parameters: { item: { pin: "a" }, mode: { pin: "on" } },
    roster: [{ id: "viewer", role: "player", kind: "scripted", binding: "baseline" }] };
  const imageDetail = { ...detail, roles: [{ ...detail.roles[0], required_modalities: ["text", "image"] }] };
  assert.ok(validateClientDesign(design, imageDetail).some((issue) => issue.path === "roster[0].capabilities"));
  const capable = { ...design, roster: [{ ...design.roster[0], capabilities: { input: ["text", "image"], evidence: "fixture" } }] };
  assert.equal(validateClientDesign(capable, imageDetail).length, 0);
  assert.equal(validateClientDesign(design, detail).length, 0);
});

test("half-written image capability declarations report instead of throwing", () => {
  const design = parseClientDesign("roster: [{id: p, role: player, kind: scripted, binding: baseline, capabilities: {input: 1, evidence: 2}}]");
  const imageDetail = { ...detail, roles: [{ ...detail.roles[0], required_modalities: ["image"] }] };
  assert.ok(validateClientDesign(design, imageDetail).some((issue) => issue.path === "roster[0].capabilities"));
});

test("typed bindings keep model and strategy choices in distinct namespaces", () => {
  const modelDetail = { ...detail, roles: [{ ...detail.roles[0], accepts: ["llm"] }] };
  for (const binding of ["baseline", { kind: "strategy", name: "baseline" }, [], 42]) {
    const design = { schema_version: 2, release: "test@v1", parameters: { item: { pin: "a" } },
      roster: [{ id: "p", role: "player", kind: "llm", binding }] };
    assert.ok(validateClientDesign(design, modelDetail).some(issue => issue.path === "roster[0].binding"));
  }
  for (const binding of [{ kind: "agent", name: "gpt-mini" },
    { kind: "model", model: "arbitrary", credential: "OPENAI_API_KEY" }]) {
    const design = { schema_version: 2, release: "test@v1", parameters: { item: { pin: "a" } },
      roster: [{ id: "p", role: "player", kind: "llm", binding }] };
    assert.deepEqual(validateClientDesign(design, modelDetail), []);
    assert.ok(validateClientDesign({ ...design, roster: [{ ...design.roster[0], kind: "scripted" }] }, detail)
      .some(issue => issue.path === "roster[0].binding"));
  }
});

test("typed inline models require explicit credential routing and reject scripted harnesses", () => {
  const modelDetail = { ...detail, roles: [{ ...detail.roles[0], accepts: ["llm"] }] };
  const design = parseClientDesign("schema_version: 2\nrelease: test@v1\nparameters: {item: {pin: a}}\nroster: [{id: p, role: player, kind: llm, harness: scripted, binding: {kind: model, model: arbitrary}}]");
  const paths = validateClientDesign(design, modelDetail).map(issue => issue.path);
  assert.ok(paths.includes("roster[0].binding.credential"));
  assert.ok(paths.includes("roster[0].harness"));
});

test("typed references defer inherited capability checks to server-side resolution", () => {
  const visual = { ...detail, roles: [{ ...detail.roles[0], accepts: ["llm"], required_modalities: ["text", "image"] }] };
  const design = parseClientDesign("schema_version: 2\nrelease: test@v1\nparameters: {item: {pin: a}}\nroster: [{id: p, role: player, kind: llm, binding: {kind: agent, name: visual}}]");
  assert.deepEqual(validateClientDesign(design, visual), []);
});
