import assert from "node:assert/strict";
import test from "node:test";
import { boundedLaunchDetail, boundedRunnerLogs, consumeLaunchEvents, LaunchStreamError, updateLaunchDetail, type LaunchStreamEvent } from "./launch-stream.ts";
import type { LaunchDetail, LaunchLog } from "./api.ts";

function log(id: number, line = `line ${id}`): LaunchLog { return { id, kind: "runner.log", payload: { line }, created_at: "2026-10-11T00:00:00Z" }; }
const detail = { launch: { id: "launch", status: "RUNNING" }, runner_logs: [log(1)], attempts: [], progress: { planned: 1, completed: 0, failed: 0, by_cell: [] } } as unknown as LaunchDetail;

test("runner tail deduplicates replayed IDs and bounds lines and text", () => {
  const logs = Array.from({ length: 1500 }, (_, index) => log(index));
  const bounded = boundedRunnerLogs([...logs, log(1499)]);
  assert.equal(bounded.length, 1000);
  assert.equal(bounded[0].id, 500);
  assert.equal(bounded.at(-1)?.id, 1499);
  const oversized = boundedRunnerLogs([log(1, "x".repeat(100000)), log(2, "most recent")]);
  assert(oversized.reduce((count, entry) => count + (entry.payload.line?.length ?? 0), 0) <= 64000);
  assert.equal(oversized.at(-1)?.payload.line, "most recent");
  assert.equal(boundedLaunchDetail({ ...detail, runner_logs: logs }).logs_truncated, true);
  assert.equal(boundedLaunchDetail({ ...detail, logs_truncated: true }).logs_truncated, true);
  assert.equal(boundedLaunchDetail(detail).logs_truncated, false);
});

test("stream updates progress and terminal status without dropping prior logs", () => {
  const next = updateLaunchDetail(detail, { kind: "runner.log", id: 2, data: log(2) as unknown as Record<string, unknown> });
  const progress = { planned: 1, completed: 1, failed: 0, by_cell: [] };
  const updated = updateLaunchDetail(next, { kind: "launch.progress", id: null, data: { progress, attempts: [{ id: "attempt", status: "COMPLETED" }], logs_truncated: true } });
  const final = updateLaunchDetail(updated, { kind: "launch.status", id: null, data: { status: "COMPLETED", ended_at: "end" } });
  assert.deepEqual(final.runner_logs.map(entry => entry.id), [1, 2]);
  assert.deepEqual(final.progress, progress);
  assert.equal(final.attempts[0].status, "COMPLETED");
  assert.equal(final.launch.status, "COMPLETED");
  assert.equal(final.logs_truncated, true);
  assert.equal(updateLaunchDetail(detail, { kind: "launch.completed", id: 3, data: {} }).launch.status, "COMPLETED");
});

test("authenticated SSE consumes split frames, reconnect replay and unnamed local events", async () => {
  const originalFetch = globalThis.fetch;
  let init: RequestInit | undefined;
  let url = "";
  const frames = `: keepalive\r\n\r\nid: 1\r\nevent: runner.log\r\ndata: ${JSON.stringify(log(1))}\r\n\r\nid: 2\r\nevent: runner.log\r\ndata: ${JSON.stringify(log(2))}\r\n\r\nid: 3\ndata: ${JSON.stringify({ id: 3, kind: "episode.reported", payload: { episode_id: "episode" } })}\n\nevent: launch.status\ndata: {"status":"COMPLETED"}\n\n`;
  const encoded = new TextEncoder().encode(frames);
  const seen: LaunchStreamEvent[] = [];
  globalThis.fetch = async (input, options) => {
    url = String(input); init = options;
    return new Response(new ReadableStream({ start(controller) {
      for (let offset = 0; offset < encoded.length; offset += 7) controller.enqueue(encoded.slice(offset, offset + 7));
      controller.close();
    } }), { headers: { "Content-Type": "text/event-stream" } });
  };
  try { await consumeLaunchEvents("/api/launches/launch/events", { afterId: 1, signal: new AbortController().signal, onEvent: event => { seen.push(event); } }); }
  finally { globalThis.fetch = originalFetch; }
  assert.equal(url, "/api/launches/launch/events?after_id=1");
  assert.equal(init?.credentials, "same-origin");
  assert.equal(new Headers(init?.headers).get("Last-Event-ID"), "1");
  assert.deepEqual(seen.map(event => [event.kind, event.id]), [["runner.log", 2], ["episode.reported", 3], ["launch.status", null]]);
});

test("terminal consumer closes the stream before later frames", async () => {
  const originalFetch = globalThis.fetch;
  let cancelled = false;
  const seen: string[] = [];
  globalThis.fetch = async () => new Response(new ReadableStream({ start(controller) {
    controller.enqueue(new TextEncoder().encode('event: launch.status\ndata: {"status":"COMPLETED"}\n\nevent: runner.log\ndata: {}\n\n'));
  }, cancel() { cancelled = true; } }), { headers: { "Content-Type": "text/event-stream" } });
  try { await consumeLaunchEvents("/events", { afterId: 0, signal: new AbortController().signal, onEvent: event => { seen.push(event.kind); return false; } }); }
  finally { globalThis.fetch = originalFetch; }
  assert.deepEqual(seen, ["launch.status"]);
  assert.equal(cancelled, true);
});

test("stream authentication failure reports a nonretryable status", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response("{}", { status: 401 });
  try {
    await assert.rejects(consumeLaunchEvents("/events", { afterId: 0, signal: new AbortController().signal, onEvent() {} }), error => error instanceof LaunchStreamError && error.status === 401);
  } finally { globalThis.fetch = originalFetch; }
});
