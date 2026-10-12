import { expect, test } from "@playwright/test";

const config = { google_client_id: "", dev_auth: true, csrf_token: "public-session-csrf" };
const principal = { owner_id: "local:alice", display_name: "Alice", email: "alice@localhost" };

test("home content is prerendered without JavaScript or authentication", async ({ browser, baseURL }) => {
  const context = await browser.newContext({ javaScriptEnabled: false, baseURL });
  const page = await context.newPage();
  try {
    await page.goto("/");
    await expect(page.locator("main h1")).toBeVisible();
    await expect(page.locator("main")).not.toContainText("Opening your workspace");
    await expect(page.locator("main")).not.toContainText("Sign in to Groundwork.");
  } finally { await context.close(); }
});

test("anonymous home stays public while private navigation requires sign-in", async ({ page }) => {
  let privateReads = 0;
  await page.route("**/auth/config", route => route.fulfill({ json: config }));
  await page.route("**/auth/me", route => route.fulfill({ status: 401, json: { error: "Sign in" } }));
  await page.route("**/api/**", route => { privateReads += 1; return route.fulfill({ json: { experiments: [], environments: [] } }); });
  const sessionResponse = page.waitForResponse(response => new URL(response.url()).pathname === "/auth/me");
  await page.goto("/");
  await sessionResponse;
  await expect(page.locator("main h1")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Sign in to Groundwork.", exact: true })).toHaveCount(0);
  await page.getByRole("navigation", { name: "Primary navigation" }).getByRole("link", { name: "Experiments", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Sign in to Groundwork.", exact: true })).toBeVisible();
  expect(privateReads).toBe(0);
});

test("stalled authentication cannot block home or expose private pages", async ({ page }) => {
  let privateReads = 0;
  let releaseConfig = () => {};
  const held = new Promise<void>(resolve => { releaseConfig = resolve; });
  await page.route("**/auth/config", async route => { await held; await route.fulfill({ json: config }); });
  await page.route("**/auth/me", route => route.fulfill({ status: 401, json: { error: "Sign in" } }));
  await page.route("**/api/**", route => { privateReads += 1; return route.fulfill({ json: { experiments: [], environments: [] } }); });
  try {
    await page.goto("/", { waitUntil: "domcontentloaded" });
    await expect(page.locator("main h1")).toBeVisible();
    await expect(page.locator("main")).not.toContainText("Opening your workspace");
    await page.getByRole("navigation", { name: "Primary navigation" }).getByRole("link", { name: "Experiments", exact: true }).click();
    await page.waitForURL("**/experiments/");
    await expect(page.locator("main .empty-state[role='status']")).toContainText("Opening your workspace");
    expect(privateReads).toBe(0);
  } finally { releaseConfig(); }
  await expect(page.getByRole("heading", { name: "Sign in to Groundwork.", exact: true })).toBeVisible();
  expect(privateReads).toBe(0);
});

test("unavailable authentication leaves home public and retains private retry", async ({ page }) => {
  let available = false;
  let privateReads = 0;
  await page.route("**/auth/config", route => route.fulfill(available ? { json: config } : { status: 503, json: { error: "Unavailable" } }));
  await page.route("**/auth/me", route => route.fulfill({ status: 401, json: { error: "Sign in" } }));
  await page.route("**/api/**", route => { privateReads += 1; return route.fulfill({ json: { experiments: [], environments: [] } }); });
  const configResponse = page.waitForResponse(response => new URL(response.url()).pathname === "/auth/config");
  await page.goto("/");
  await configResponse;
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(page.locator("main h1")).toBeVisible();
  await expect(page.locator("main")).not.toContainText("Sign-in configuration is unavailable.");
  await page.getByRole("navigation", { name: "Primary navigation" }).getByRole("link", { name: "Experiments", exact: true }).click();
  await expect(page.locator(".session-card").getByRole("alert")).toContainText("Sign-in configuration is unavailable.");
  available = true;
  await page.getByRole("button", { name: "Try again", exact: true }).click();
  await expect(page.getByRole("button", { name: "Continue as Alice", exact: true })).toBeVisible();
  expect(privateReads).toBe(0);
});

test("sign-out on home preserves public content and closes private navigation", async ({ page }) => {
  let signedIn = true;
  let privateReads = 0;
  const logoutHeaders: string[] = [];
  await page.route("**/auth/config", route => route.fulfill({ json: config }));
  await page.route("**/auth/me", route => route.fulfill(signedIn ? { json: { user: principal } } : { status: 401, json: { error: "Sign in" } }));
  await page.route("**/auth/logout", route => {
    logoutHeaders.push(route.request().headers()["x-csrf-token"]);
    signedIn = false;
    return route.fulfill({ json: { ok: true } });
  });
  await page.route("**/api/**", route => { privateReads += 1; return route.fulfill({ json: { experiments: [], environments: [] } }); });
  await page.goto("/");
  await expect(page.getByRole("region", { name: "Your account" })).toContainText("Alice");
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.getByRole("region", { name: "Your account" })).toHaveCount(0);
  await expect(page.locator("main h1")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Sign in to Groundwork.", exact: true })).toHaveCount(0);
  await page.getByRole("navigation", { name: "Primary navigation" }).getByRole("link", { name: "Experiments", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Sign in to Groundwork.", exact: true })).toBeVisible();
  expect(privateReads).toBe(0);
  expect(logoutHeaders).toEqual([config.csrf_token]);
});
