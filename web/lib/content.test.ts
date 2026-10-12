import assert from "node:assert/strict";
import test from "node:test";
import { contentText } from "./content.ts";

test("structured text survives without stringifying private image metadata", () => {
  const content = [{ type: "text", text: "look" }, { type: "image", sha256: "private-digest", data: "private-bytes" }, { type: "text", text: "here" }];
  assert.equal(contentText(content), "look\nhere");
  assert.equal(contentText("legacy text"), "legacy text");
  assert.equal(contentText([{ type: "image", data: "private" }]), null);
});
