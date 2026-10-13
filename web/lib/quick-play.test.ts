import assert from "node:assert/strict";
import test from "node:test";
import { parse } from "yaml";
import type { AgentBinding, EnvironmentDetail, LaunchDetail } from "./api.ts";
import { compatibleModels, parameterOptions, publishedQuickPlayTrace, quickPlayDesign, scenarioPresets } from "./quick-play.ts";

const detail: EnvironmentDetail = {
  environment_id: "test", release: { release_id: "release", version: "v1", declaration_sha256: "x", item_bank_sha256: "y", oracle_version: null },
  roles: [{ id: "player", count: 2, accepts: ["scripted", "llm"], description: "" }],
  parameters: [
    { name: "difficulty", type: "categorical", domain: ["easy", "hard"], source: "item", item_key: "level", fixed: false, levels: [{ value: "easy", count: 2 }, { value: "hard", count: 2 }], identity_grained: false },
    { name: "turns", type: "integer", domain: [1, 50], source: "design", item_key: null, fixed: false, levels: null, identity_grained: false },
  ], measures: [], item_policy: { mode: "enumerate", bank_path: "bank", item_bank_sha256: "y", item_count: 4 }, declaration_yaml: "",
};

test("quick play produces one episode, complete declared seats, and pins instead of factors", () => {
  const design = parse(quickPlayDesign(detail, { turns: 5 }, { item_id: "item", params: {}, parameter_values: { difficulty: "hard" }, oracle_result: null }, "smoke", {}, []));
  assert.deepEqual(design.units, { episodes_per_cell: 1 });
  assert.deepEqual(design.parameters, { difficulty: { pin: "hard" }, turns: { pin: 5 } });
  assert.deepEqual(design.roster.map((seat: { id: string; kind: string }) => [seat.id, seat.kind]), [["player_1", "scripted"], ["player_2", "scripted"]]);
  assert.equal(design.release, "release");
});

test("automatic scenarios have explicit item dispositions and retain environment defaults", () => {
  const design = parse(quickPlayDesign(detail, {}, undefined, "smoke", {}, []));
  assert.deepEqual(design.parameters, { difficulty: { randomize: true } });
  assert.throws(() => quickPlayDesign(detail, {}, { item_id: "broken", params: {}, oracle_result: null }, "smoke", {}, []), /missing difficulty/);
});

test("visual roles admit verified image models and copy binding capabilities into the roster", () => {
  const visual: EnvironmentDetail = { ...detail, required_modalities: ["text", "image"] };
  const agents = [
    { name: "text", type: "llm", model: "text" },
    { name: "unverified", type: "llm", capabilities: { input: ["text", "image"] } },
    { name: "visual", type: "llm", model: "visual", api_format: "openai", capabilities: { input: ["text", "image"], evidence: "documentation" } },
  ] as AgentBinding[];
  assert.deepEqual(compatibleModels(visual, visual.roles[0], agents).map(agent => agent.name), ["visual"]);
  assert.throws(() => quickPlayDesign(visual, {}, undefined, "live", { player_1: "text" }, agents), /compatible model/);
  const design = parse(quickPlayDesign(visual, {}, undefined, "live", { player_1: "visual", player_2: "visual" }, agents));
  assert.equal(design.roster[0].api_format, "openai");
  assert.deepEqual(design.roster[0].capabilities, agents[2].capabilities);
});

test("numeric presets remain inside the declared domain", () => {
  assert(parameterOptions(detail.parameters[1]).every(value => Number.isInteger(value) && Number(value) >= 1 && Number(value) <= 50));
});

test("scenario presets group equivalent items without promising a sampled row identity", () => {
  const presets = scenarioPresets(detail, [
    { item_id: "a", params: { level: "easy" }, oracle_result: null },
    { item_id: "b", params: { level: "easy" }, oracle_result: null },
    { item_id: "c", params: { level: "hard" }, oracle_result: null },
  ]);
  assert.deepEqual(presets.map(preset => preset.label), ["Difficulty: easy", "Difficulty: hard"]);
});

test("trace navigation waits for final publication and never turns failed evidence into success", () => {
  const launch = { launch: { status: "COMPLETED" }, progress: { completed: 1, failed: 0 }, attempts: [{ status: "COMPLETED", episode_uid: "trace" }] } as LaunchDetail;
  assert.equal(publishedQuickPlayTrace(launch), "trace");
  assert.equal(publishedQuickPlayTrace({ ...launch, provisional: true }), null);
  assert.equal(publishedQuickPlayTrace({ ...launch, launch: { ...launch.launch, status: "RUNNING" } }), null);
  assert.equal(publishedQuickPlayTrace({ ...launch, attempts: [{ ...launch.attempts[0], status: "FAILED" }] }), null);
});
