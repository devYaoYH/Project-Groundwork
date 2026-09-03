import assert from "node:assert/strict";
import test from "node:test";

import { getEnvironment, getItems, runOracle } from "./api.ts";

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
