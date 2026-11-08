import assert from "node:assert/strict";
import test from "node:test";
import { parse } from "yaml";
import type { EnvironmentDetail } from "./api.ts";
import { initialDesign } from "./design-template.ts";
import { parseClientDesign, validateClientDesign } from "./validate.ts";

const detail: EnvironmentDetail = {
  environment_id: "test", release: { release_id: "test@v1", version: "v1",
    declaration_sha256: "x", item_bank_sha256: "y", oracle_version: null },
  parameters: [], roles: [
    { id: "model", count: 1, accepts: ["llm"], description: "" },
    { id: "script", count: 1, accepts: ["llm", "scripted"], description: "" },
    { id: "person", count: 1, accepts: ["human"], description: "" },
  ],
  measures: [], item_policy: { mode: "enumerate", bank_path: "items.jsonl", item_bank_sha256: "y", item_count: 1 },
  declaration_yaml: "",
};

test("new drafts use typed strategies and leave model selection incomplete", () => {
  const text = initialDesign(detail);
  const design = parse(text);
  assert.equal(design.schema_version, 2);
  assert.deepEqual(design.roster.map((seat: { binding: unknown }) => seat.binding),
    [null, { kind: "strategy", name: "baseline" }, null]);
  const errors = validateClientDesign(parseClientDesign(text), detail);
  assert.deepEqual(errors.map(issue => issue.path), ["roster[0].binding"]);
});

test("editing a typed binding one character at a time never throws", () => {
  const text = initialDesign(detail).replace("binding: null", "binding: {kind: agent, name: gpt-mini}");
  for (let index = 0; index <= text.length; index += 1) {
    assert.doesNotThrow(() => validateClientDesign(parseClientDesign(text.slice(0, index)), detail));
  }
});
