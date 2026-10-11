import { expect, test } from "@playwright/test";
import { readFile } from "node:fs/promises";

test("authenticated shell keeps navigation and clears the prior user's workspace", async ({ page }) => {
  let user: string | null = null;
  let privateReads = 0;
  const mutations: string[] = [];
  const principal = () => ({ owner_id: `local:${user?.toLowerCase()}`, display_name: user, email: `${user}@localhost` });
  await page.route("**/auth/config", route => route.fulfill({ json: { google_client_id: "", dev_auth: true, csrf_token: "test-csrf-challenge" } }));
  await page.route("**/auth/me", route => route.fulfill({ status: user ? 200 : 401, json: user ? { user: principal() } : { error: "Sign in" } }));
  await page.route("**/auth/dev", route => {
    mutations.push(route.request().headers()["x-csrf-token"]);
    user = route.request().postDataJSON().user;
    return route.fulfill({ json: { user: principal() } });
  });
  await page.route("**/auth/logout", route => {
    mutations.push(route.request().headers()["x-csrf-token"]); user = null;
    return route.fulfill({ json: { ok: true } });
  });
  await page.route("**/api/environments", route => route.fulfill({ json: { environments: [] } }));
  await page.route("**/api/experiments", route => {
    privateReads += 1;
    return route.fulfill({ json: { experiments: [{ id: `${user}-experiment`, name: `${user}'s private experiment`,
      release_id: "buyer_seller@v1", design_text: "design", locked_at: null, created_at: "2026-10-11T00:00:00Z" }] } });
  });
  await page.goto("/experiments/");
  await expect(page.getByRole("heading", { name: "Sign in to Groundwork." })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Primary navigation" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Environments", exact: true })).toBeVisible();
  expect(privateReads).toBe(0);
  await page.getByRole("button", { name: "Continue as Alice" }).click();
  await expect(page.getByRole("heading", { name: "Your experiments" })).toBeVisible();
  await expect(page.getByText("Alice's private experiment", { exact: true })).toBeVisible();
  await expect(page.getByRole("region", { name: "Your account" })).toContainText("Alice");
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Sign in to Groundwork." })).toBeVisible();
  await expect(page.getByText("Alice's private experiment", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Continue as Bob" }).click();
  await expect(page.getByText("Bob's private experiment", { exact: true })).toBeVisible();
  await expect(page.getByText("Alice's private experiment", { exact: true })).toHaveCount(0);
  expect(mutations).toEqual(["test-csrf-challenge", "test-csrf-challenge", "test-csrf-challenge"]);
});

test("auth configuration failure keeps protected pages closed", async ({ page }) => {
  let privateReads = 0;
  await page.route("**/auth/config", route => route.fulfill({ status: 503, json: { error: "Unavailable" } }));
  await page.route("**/api/experiments", route => { privateReads += 1; return route.fulfill({ json: { experiments: [] } }); });
  await page.goto("/experiments/");
  await expect(page.locator(".session-card").getByRole("alert")).toContainText("Sign-in configuration is unavailable.");
  await expect(page.getByRole("button", { name: "Try again" })).toBeVisible();
  expect(privateReads).toBe(0);
});

for (const platform of [true, false]) {
  test(`${platform ? "platform" : "original local"} draft launch affordances respect preregistration`, async ({ page }) => {
    let locked = false;
    await page.route("**/auth/config", route => route.fulfill(platform
      ? { json: { google_client_id: "", dev_auth: true, csrf_token: "test-csrf" } }
      : { status: 404, json: { error: "Not found" } }));
    await page.route("**/auth/me", route => route.fulfill({ json: { user: { owner_id: "local:alice", display_name: "Alice", email: "alice@localhost" } } }));
    await page.route("**/api/environments/buyer_seller", route => route.fulfill({ json: {
      environment_id: "buyer_seller", release: { version: "v1", release_id: "buyer_seller@v1" }, parameters: [], roles: [],
    } }));
    await page.route("**/api/experiments/draft-test", route => route.fulfill({ json: {
      experiment: { id: "draft-test", name: "Draft launch test", environment_id: "buyer_seller", release_id: "buyer_seller@v1",
        design_text: "release: buyer_seller@v1\nparameters: {}\nroster: []\n", locked_at: locked ? "2026-10-11T00:00:00Z" : null },
      cells: [], cell_evidence: [], roster: [], launches: [],
    } }));
    await page.route("**/api/designs/validate", route => route.fulfill({ json: {
      valid: true, errors: [], plan: { cells: [], episodes_planned: 1, preview_episode_config: null },
    } }));
    await page.goto("/design/?id=draft-test");
    await expect(page.getByRole("button", { name: "Lock preregistration", exact: true })).toBeEnabled();
    for (const name of ["Smoke", "Dry run"]) {
      if (platform) await expect(page.getByRole("button", { name, exact: true })).toBeDisabled();
      else await expect(page.getByRole("button", { name, exact: true })).toBeEnabled();
    }
    await expect(page.getByRole("button", { name: "Launch live", exact: true })).toBeDisabled();
    if (platform) await expect(page.locator(".launch-card")).toContainText("Lock the preregistration before any launch");
    locked = true;
    await page.reload();
    for (const name of ["Smoke", "Dry run", "Launch live"]) {
      await expect(page.getByRole("button", { name, exact: true })).toBeEnabled();
    }
    await expect(page.getByRole("button", { name: "Locked", exact: true })).toBeDisabled();
  });
}

test("prepared exports download through a reusable private ZIP link", async ({ page }) => {
  let exports = 0;
  await page.route("**/auth/config", route => route.fulfill({ json: { google_client_id: "", dev_auth: true, csrf_token: "export-csrf" } }));
  await page.route("**/auth/me", route => route.fulfill({ json: { user: { owner_id: "local:alice", display_name: "Alice", email: "alice@localhost" } } }));
  await page.route("**/api/experiments/export-test", route => route.fulfill({ json: {
    experiment: { id: "export-test", name: "Export download test", release_id: "buyer_seller@v1", locked_at: null },
    cells: [], cell_evidence: [], roster: [], launches: [],
  } }));
  await page.route("**/api/episodes?*", route => route.fulfill({ json: { episodes: [] } }));
  await page.route("**/api/experiments/export-test/export", route => {
    expect(route.request().headers()["x-csrf-token"]).toBe("export-csrf");
    exports += 1;
    return route.fulfill({ json: { id: "browser-test-export", download_url: "/api/exports/browser-test-export/download" } });
  });
  await page.goto("/experiment/?id=export-test");
  await page.getByRole("button", { name: "Download results", exact: true }).click();
  const link = page.getByRole("link", { name: "Download ZIP", exact: true });
  await expect(link).toHaveAttribute("href", "/api/exports/browser-test-export/download");
  for (let retry = 0; retry < 2; retry += 1) {
    const downloaded = page.waitForEvent("download");
    await link.click();
    const download = await downloaded;
    expect(download.suggestedFilename()).toBe("experiment-export-test.zip");
    expect(await download.failure()).toBeNull();
    const downloadedPath = await download.path();
    expect((await readFile(downloadedPath!)).length).toBe(22);
  }
  expect(exports).toBe(1);
});
