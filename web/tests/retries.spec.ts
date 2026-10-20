import { expect, test, type Page } from "@playwright/test";

const launch = { id: "partial", experiment_id: "study", status: "PARTIAL", mode: "smoke", max_parallelism: 2,
  created_at: "2026-10-20T00:00:00Z", started_at: null, ended_at: null, trace_database: "private.db" };
const attempts = ["completed", "failed", "missing"].map((episode_id, index) => ({
  id: episode_id, launch_id: launch.id, episode_id, cell_id: "cell", episode_idx: index, attempt: 1,
  status: index === 0 ? "COMPLETED" : index === 1 ? "FAILED" : "UNREPORTED", episode_uid: index === 1 ? "failed-trace" : null,
}));
const progress = { planned: 3, completed: 1, failed: 2, by_cell: [{ cell_id: "cell", planned: 3, completed: 1, failed: 2 }] };

async function fixtures(page: Page, retryable = true) {
  const bodies: Record<string, unknown>[] = [];
  await page.route("**/auth/config", route => route.fulfill({ json: { google_client_id: "", dev_auth: true, csrf_token: "retry-csrf" } }));
  await page.route("**/auth/me", route => route.fulfill({ json: { user: { owner_id: "local:alice", display_name: "Alice" } } }));
  await page.route("**/api/experiments/study", route => route.fulfill({ json: {
    experiment: { id: "study", name: "Partially launched study", release_id: "buyer_seller@v1", locked_at: launch.created_at },
    cells: [{ cell_id: "cell", levels: {}, episodes_planned: 3 }], cell_evidence: [], roster: [],
    launches: [{ launch, progress }],
    retry_options: retryable ? [{ mode: "smoke", episode_ids: ["failed", "missing"], max_parallelism: 2 }] : [],
  } }));
  await page.route("**/api/episodes?*", route => route.fulfill({ json: { episodes: [
    { episode_uid: "failed-trace", episode_id: "failed", cell_id: "cell", episode_idx: 1, attempt: 1, status: "PARTIAL" },
    { episode_uid: "completed-trace", episode_id: "completed", cell_id: "cell", episode_idx: 0, attempt: 1, status: "COMPLETED" },
  ] } }));
  await page.route("**/api/episodes/failed-trace", route => route.fulfill({ json: {
    episode: { episode_uid: "failed-trace", config: { environment_id: "buyer_seller", provenance: { experiment_id: "study", episode_id: "failed" } },
      events: [], observability: { partial: true }, metrics: {}, stopped: true }, lanes: [], cursor_max: 0,
  } }));
  await page.route("**/api/launches/*", route => route.fulfill({ json: {
    launch: { ...launch, id: route.request().url().split("/").at(-1) }, attempts, progress, runner_logs: [],
  } }));
  await page.route("**/api/launches", route => {
    bodies.push(route.request().postDataJSON());
    return route.fulfill({ status: 202, json: { ...launch, id: "new-retry" } });
  });
  return bodies;
}

test("experiment retry skips completed replications and opens the new launch", async ({ page }) => {
  const bodies = await fixtures(page);
  await page.goto("/experiment/?id=study");
  await page.getByRole("button", { name: "Retry incomplete · smoke", exact: true }).click();
  await expect(page).toHaveURL(/\/launch\/\?id=new-retry/);
  expect(bodies).toEqual([{ experiment_id: "study", mode: "smoke", max_parallelism: 2,
    retry_incomplete: true, episode_ids: ["failed", "missing"] }]);
});

test("launch retries an episode even when no trace was persisted", async ({ page }) => {
  const bodies = await fixtures(page);
  await page.goto("/launch/?id=partial");
  await expect(page.getByRole("button", { name: "Retry incomplete · smoke", exact: true })).toBeVisible();
  await expect(page.getByRole("row").filter({ has: page.getByText("completed", { exact: true }) }).getByRole("button")).toHaveCount(0);
  await page.getByRole("row").filter({ has: page.getByText("missing", { exact: true }) }).getByRole("button", { name: "Retry episode · smoke", exact: true }).click();
  await expect(page).toHaveURL(/id=new-retry/);
  expect(bodies[0].episode_ids).toEqual(["missing"]);
});

test("partial episode offers an individual retry", async ({ page }) => {
  const bodies = await fixtures(page);
  await page.goto("/episode/?uid=failed-trace");
  await page.getByRole("button", { name: "Retry episode · smoke", exact: true }).click();
  await expect(page).toHaveURL(/id=new-retry/);
  expect(bodies[0].episode_ids).toEqual(["failed"]);
});

test("retry errors preserve evidence and re-enable the action", async ({ page }) => {
  await fixtures(page);
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/launches", async route => {
    await pending;
    return route.fulfill({ status: 400, json: { error: "Pinned runtime is unavailable" } });
  });
  await page.goto("/experiment/?id=study");
  await page.getByRole("button", { name: "Retry incomplete · smoke", exact: true }).click();
  await expect(page.getByRole("button", { name: "Starting retry…", exact: true })).toBeDisabled();
  release();
  await expect(page.getByRole("alert").filter({ hasText: "Pinned runtime is unavailable" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry incomplete · smoke", exact: true })).toBeEnabled();
  await expect(page.getByRole("heading", { name: "Partially launched study", exact: true })).toBeVisible();
});

test("retry actions disappear when no eligible episodes remain or a launch is active", async ({ page }) => {
  await fixtures(page, false);
  await page.goto("/experiment/?id=study");
  await expect(page.getByRole("heading", { name: "Partially launched study", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /Retry/ })).toHaveCount(0);
  await page.goto("/episode/?uid=failed-trace");
  await expect(page.getByRole("heading", { name: "failed", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /Retry/ })).toHaveCount(0);
});
