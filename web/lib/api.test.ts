import assert from "node:assert/strict";
import test from "node:test";

import { getEnvironment, getItems, runOracle, saveDesign } from "./api.ts";

test("Word-Guess environment navigation preserves its API identifier", async () => {
  const originalFetch = globalThis.fetch;
  const paths: string[] = [];
  globalThis.fetch = async (input) => {
    paths.push(String(input));
    return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
  };

  try {
    await getEnvironment("word_guess");
    await getItems("word_guess");
    await runOracle("word_guess", "word-001");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.deepEqual(paths, [
    "/api/environments/word_guess",
    "/api/environments/word_guess/items?limit=100",
    "/api/environments/word_guess/oracle",
  ]);
  assert(paths.every((path) => !path.includes("undefined")));
});

test("environment requests reject a missing identifier before fetching", async () => {
  const originalFetch = globalThis.fetch;
  let fetchCalls = 0;
  globalThis.fetch = async () => {
    fetchCalls += 1;
    return new Response("{}", { status: 200 });
  };

  try {
    assert.throws(
      () => getEnvironment(undefined as unknown as string),
      /concrete environment id/,
    );
    assert.throws(() => getItems(" undefined "), /concrete environment id/);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(fetchCalls, 0);
});

test("saveDesign sends the draft text to its experiment endpoint", async () => {
  const originalFetch = globalThis.fetch;
  let path = "";
  let init: RequestInit | undefined;
  globalThis.fetch = async (input, requestInit) => {
    path = String(input);
    init = requestInit;
    return new Response(JSON.stringify({ id: "experiment id" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  try {
    await saveDesign("experiment id", "schema_version: 1\n");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(path, "/api/experiments/experiment%20id/design");
  assert.equal(init?.method, "POST");
  assert.equal(init?.body, JSON.stringify({ design_text: "schema_version: 1\n" }));
});
