import assert from "node:assert/strict";
import test from "node:test";

import {
  forkDesign, getEnvironment, getItems, listEpisodes, runOracle, saveDesign, subscribeLaunchEvents,
} from "./api.ts";

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

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  onmessage: ((frame: MessageEvent) => void) | null = null;
  closed = false;
  url: string;
  constructor(url: string) { this.url = url; FakeEventSource.instances.push(this); }
  close() { this.closed = true; }
  emit(data: string) { this.onmessage?.({ data } as MessageEvent); }
}

test("subscribeLaunchEvents listens for unnamed frames and dispatches on the payload kind", () => {
  const original = (globalThis as { EventSource?: unknown }).EventSource;
  (globalThis as { EventSource?: unknown }).EventSource = FakeEventSource;
  FakeEventSource.instances = [];
  const received: string[] = [];
  try {
    const close = subscribeLaunchEvents("launch id/1", (entry) => received.push(entry.kind));
    const source = FakeEventSource.instances[0];
    assert.equal(source.url, "/api/launches/launch%20id%2F1/events");

    source.emit(JSON.stringify({ id: 1, kind: "launch.queued", payload: {}, created_at: "" }));
    // A kind no client has heard of still arrives: frames are unnamed on purpose.
    source.emit(JSON.stringify({ id: 2, kind: "attempt.progress", payload: { episode_id: "e", durable_through: 3 }, created_at: "" }));
    source.emit("{not json");
    source.emit(JSON.stringify({ id: 3 }));
    assert.deepEqual(received, ["launch.queued", "attempt.progress"]);

    close();
    assert.equal(source.closed, true);
  } finally {
    (globalThis as { EventSource?: unknown }).EventSource = original;
  }
});

test("subscribeLaunchEvents is a no-op where EventSource does not exist", () => {
  const original = (globalThis as { EventSource?: unknown }).EventSource;
  delete (globalThis as { EventSource?: unknown }).EventSource;
  try {
    const close = subscribeLaunchEvents("launch", () => assert.fail("no stream exists"));
    close();
  } finally {
    (globalThis as { EventSource?: unknown }).EventSource = original;
  }
});
