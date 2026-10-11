import assert from "node:assert/strict";
import test from "node:test";

import { createDesignExperiment, forkDesign, getEnvironment, getItems, listEpisodes, prepareExperimentExport, runOracle, saveDesign } from "./api.ts";
import { setSessionCsrf } from "./session.ts";

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

test("forkDesign uses the explicit fork endpoint", async () => {
  const originalFetch = globalThis.fetch;
  let path = "";
  let init: RequestInit | undefined;
  globalThis.fetch = async (input, requestInit) => {
    path = String(input);
    init = requestInit;
    return new Response(JSON.stringify({ id: "fork id" }), {
      status: 201,
      headers: { "Content-Type": "application/json" },
    });
  };

  try {
    await forkDesign("experiment id");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(path, "/api/experiments/experiment%20id/fork");
  assert.equal(init?.method, "POST");
  assert.equal(init?.body, "{}");
});

test("listEpisodes preserves repeated environment and status filters", async () => {
  const originalFetch = globalThis.fetch;
  let path = "";
  globalThis.fetch = async (input) => {
    path = String(input);
    return new Response(JSON.stringify({ episodes: [], next_cursor: null, filters: {}, facets: { environments: [], statuses: [] } }), {
      status: 200, headers: { "Content-Type": "application/json" },
    });
  };

  try {
    await listEpisodes({ q: "testing fork", environment_id: ["word_guess", "calendar"], status: ["COMPLETED", "PARTIAL"] });
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(path, "/api/episodes?limit=50&q=testing+fork&environment_id=word_guess&environment_id=calendar&status=COMPLETED&status=PARTIAL");
});

test("new browser designs stay editable and include the session CSRF challenge", async () => {
  const originalFetch = globalThis.fetch;
  let init: RequestInit | undefined;
  globalThis.fetch = async (_, options) => { init = options; return new Response(JSON.stringify({ id: "draft" }), { status: 201 }); };
  setSessionCsrf("public-csrf-challenge");
  try { await createDesignExperiment("My design", "buyer_seller@v1", "schema_version: 1\n"); }
  finally { globalThis.fetch = originalFetch; setSessionCsrf(""); }
  assert.equal(init?.credentials, "same-origin");
  assert.equal(new Headers(init?.headers).get("X-CSRF-Token"), "public-csrf-challenge");
  assert.equal(JSON.parse(String(init?.body)).draft, true);
});

test("export preparation returns a same-origin link without fetching the ZIP", async () => {
  const originalFetch = globalThis.fetch;
  const paths: string[] = [];
  let init: RequestInit | undefined;
  globalThis.fetch = async (input, options) => {
    paths.push(String(input)); init = options;
    return new Response(JSON.stringify({ id: "export id", download_url: "https://other.example/unused.zip" }), { status: 201 });
  };
  setSessionCsrf("export-csrf");
  try {
    assert.deepEqual(await prepareExperimentExport("experiment id"), { id: "export id", download_url: "/api/exports/export%20id/download" });
  } finally { globalThis.fetch = originalFetch; setSessionCsrf(""); }
  assert.deepEqual(paths, ["/api/experiments/experiment%20id/export"]);
  assert.equal(init?.method, "POST");
  assert.equal(init?.credentials, "same-origin");
  assert.equal(new Headers(init?.headers).get("X-CSRF-Token"), "export-csrf");
});
