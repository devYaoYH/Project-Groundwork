import { expect, test } from "@playwright/test";

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
