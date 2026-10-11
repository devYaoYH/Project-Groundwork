import { expect, test } from "@playwright/test";

test("live launch logs reconnect, deduplicate and finish without full-detail polling", async ({ page }) => {
  let detailReads = 0;
  const cursors: string[] = [];
  const log = (id: number, line: string) => ({ id, kind: "runner.log", payload: { line }, created_at: "2026-10-11T00:00:00Z" });
  const frame = (kind: string, data: object, id?: number) => `${id ? `id: ${id}\n` : ""}event: ${kind}\ndata: ${JSON.stringify(data)}\n\n`;
  const attempt = { id: "attempt", episode_id: "episode-1", cell_id: "cell", episode_idx: 0, status: "COMPLETED", episode_uid: "trace-1" };
  const progress = { planned: 1, completed: 0, failed: 0, by_cell: [{ cell_id: "cell", planned: 1, completed: 0, failed: 0 }] };
  await page.route("**/auth/config", route => route.fulfill({ json: { google_client_id: "", dev_auth: true, csrf_token: "live-csrf" } }));
  await page.route("**/auth/me", route => route.fulfill({ json: { user: { owner_id: "local:alice", display_name: "Alice", email: "alice@localhost" } } }));
  await page.route("**/api/launches/live-test", route => {
    detailReads += 1;
    return route.fulfill({ json: { launch: { id: "live-test", experiment_id: "experiment", status: "RUNNING", mode: "smoke", max_parallelism: 1,
      created_at: "2026-10-11T00:00:00Z", started_at: "2026-10-11T00:00:00Z", ended_at: null, live_logs: true },
      attempts: [attempt], progress, provisional: true, runner_logs: [log(1, "first persisted line")],
    } });
  });
  await page.route("**/api/launches/live-test/events?*", route => {
    cursors.push(new URL(route.request().url()).searchParams.get("after_id") ?? "");
    if (cursors.length === 1) return route.fulfill({ status: 503, json: { error: "Temporary stream outage" } });
    const body = cursors.length === 2
      ? frame("runner.log", log(2, "incremental line"), 2) + frame("launch.progress", { progress: { ...progress, completed: 1 }, attempts: [attempt], provisional: true })
      : frame("runner.log", log(2, "incremental line"), 2) + frame("runner.log", log(3, "final line before completion"), 3)
        + frame("launch.progress", { progress: { ...progress, completed: 1 }, attempts: [attempt], provisional: false, logs_truncated: true })
        + frame("launch.status", { id: "live-test", status: "COMPLETED", ended_at: "2026-10-11T00:01:00Z" });
    return route.fulfill({ body, contentType: "text/event-stream" });
  });
  await page.goto("/launch/?id=live-test");
  const output = page.getByLabel("Runner output");
  const runner = page.locator(".section-card").filter({ has: page.getByRole("heading", { name: "Runner log", exact: true }) });
  await expect(runner.getByRole("alert")).toContainText("Live logs are unavailable (503)");
  await expect(output).toContainText("first persisted line");
  await expect(page.getByText("Open episode", { exact: true })).toHaveCount(0);
  await expect(output).toContainText("incremental line");
  await expect(page.getByText("Live progress is provisional.", { exact: false })).toBeVisible();
  await expect(output).toContainText("final line before completion");
  await expect(runner.getByRole("status")).toContainText("Launch ended · logs saved");
  await expect(runner.getByRole("alert")).toHaveCount(0);
  await expect(page.getByText("Open episode", { exact: true })).toBeVisible();
  await expect(page.getByText("Earlier output is omitted here.", { exact: false })).toBeVisible();
  expect((await output.textContent())?.match(/incremental line/g)).toHaveLength(1);
  expect(cursors).toEqual(["1", "1", "2"]);
  expect(detailReads).toBe(1);
  await page.waitForTimeout(2300);
  expect(cursors).toHaveLength(3);
  expect(detailReads).toBe(1);
});

test("earlier release launches explain why live logs are unavailable", async ({ page }) => {
  await page.route("**/auth/config", route => route.fulfill({ json: { google_client_id: "", dev_auth: true, csrf_token: "legacy-csrf" } }));
  await page.route("**/auth/me", route => route.fulfill({ json: { user: { owner_id: "local:alice", display_name: "Alice", email: "alice@localhost" } } }));
  await page.route("**/api/launches/legacy-test", route => route.fulfill({ json: {
    launch: { id: "legacy-test", experiment_id: "experiment", status: "RUNNING", mode: "smoke", max_parallelism: 1, live_logs: false },
    attempts: [], runner_logs: [], progress: { planned: 1, completed: 0, failed: 0, by_cell: [] },
  } }));
  await page.route("**/api/launches/legacy-test/events?*", route => route.fulfill({ body: 'event: launch.status\ndata: {"status":"RUNNING"}\n\n', contentType: "text/event-stream" }));
  await page.goto("/launch/?id=legacy-test");
  await expect(page.getByText("This experiment uses an earlier release;", { exact: false })).toContainText("Fork the design to use live logs.");
});
