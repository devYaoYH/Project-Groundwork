import { expect, test } from "@playwright/test";
import { environmentIds } from "../lib/environments";

for (const width of [390, 1280]) for (const colorScheme of ["light", "dark"] as const) {
  test(`Quick Play cards and single episode at ${width}px ${colorScheme}`, async ({ page, request }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ colorScheme });
    await page.goto("/environments/");
    const card = page.getByRole("article").filter({ has: page.getByRole("heading", { name: "Word Guess" }) });
    await expect(card.getByRole("link", { name: "Details Word Guess" })).toHaveAttribute("href", "/environments/word_guess/");
    await card.getByRole("link", { name: "Quick Play Word Guess" }).click();
    await expect(page.getByLabel("Scenario", { exact: true })).toBeVisible();
    await expect(page.locator("textarea, input[type=text]")).toHaveCount(0);
    await page.getByLabel("Max Turns").selectOption("1");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    await page.getByRole("button", { name: "Run episode", exact: true }).click();
    await expect(page).toHaveURL(/\/episode\/\?uid=/, { timeout: 60000 });
    const uid = new URL(page.url()).searchParams.get("uid")!;
    const episode = await (await request.get(`/api/episodes/${uid}`)).json();
    expect(episode.episode.config.max_turns).toBe(1);
    const experimentId = episode.episode.config.provenance.experiment_id;
    const experiment = await (await request.get(`/api/experiments/${experimentId}`)).json();
    expect(experiment.cells).toHaveLength(1);
    expect(experiment.cells[0].episodes_planned).toBe(1);
    expect(experiment.experiment.locked_at).toBeTruthy();
  });
}

test("models are an explicit choice and visual roles filter incompatible bindings", async ({ page }) => {
  test.skip(!environmentIds.includes("tangram_reference"), "Requires an installed visual environment.");
  await page.goto("/environments/tangram_reference/play/");
  await page.getByLabel("Run with").selectOption("live");
  await expect(page.getByLabel("Director", { exact: true })).toContainText("ds-v4.1-flash");
  const options = await page.getByLabel("Director", { exact: true }).locator("option").allTextContents();
  expect(options.some(option => option.includes("ds-v4.1-flash"))).toBeTruthy();
  expect(options.some(option => option.includes("gpt-mini"))).toBeFalsy();
  await expect(page.getByText(/may incur usage charges/)).toBeVisible();
});

test("a rejected setup creates no run and keeps settings editable", async ({ page }) => {
  let created = 0;
  page.on("request", request => { if (request.method() === "POST" && request.url().endsWith("/api/experiments")) created += 1; });
  await page.route("**/api/designs/validate", route => route.fulfill({ contentType: "application/json", body: JSON.stringify({ valid: false, errors: [{ path: "parameters", message: "Controlled setup rejection" }], plan: null }) }));
  await page.goto("/environments/word_guess/play/");
  await page.getByRole("button", { name: "Run episode", exact: true }).click();
  await expect(page.locator("main .notice[role=alert]")).toContainText("Controlled setup rejection");
  await expect(page.getByLabel("Max Turns")).toBeEnabled();
  expect(created).toBe(0);
});

for (const id of environmentIds.filter(id => id !== "word_guess")) {
  test(`selected scenario runs a single persisted ${id} episode`, async ({ page, request }) => {
    test.setTimeout(90000);
    const items = await (await request.get(`/api/environments/${id}/items`)).json();
    await page.goto(`/environments/${id}/play/`);
    await page.getByLabel("Scenario", { exact: true }).selectOption(items.items[0].item_id);
    if (id === "negotiation") {
      await page.getByLabel("Num Rounds", { exact: true }).selectOption("1");
      await page.getByLabel("Cheap Talk Turns", { exact: true }).selectOption("0");
    }
    await page.getByRole("button", { name: "Run episode", exact: true }).click();
    await expect(page).toHaveURL(/\/episode\/\?uid=/, { timeout: 60000 });
    const uid = new URL(page.url()).searchParams.get("uid")!;
    const result = await (await request.get(`/api/episodes/${uid}`)).json();
    expect(result.episode.config.provenance.item_id).toBeTruthy();
    if (id === "negotiation") {
      const selected = items.items.find((item: { item_id: string }) => item.item_id === result.episode.config.provenance.item_id);
      expect(selected).toBeTruthy();
      expect(result.episode.config.agent_projects).toEqual(selected.params.agent_projects);
      expect(result.episode.config.oracle_stats).toEqual(selected.oracle_result);
      expect(result.episode.config.item_bank_sha256).toBe(items.item_bank_sha256);
    }
    const experiment = await (await request.get(`/api/experiments/${result.episode.config.provenance.experiment_id}`)).json();
    expect(experiment.cells).toHaveLength(1);
    expect(experiment.cells[0].episodes_planned).toBe(1);
    for (const [name, value] of Object.entries(items.items[0].parameter_values)) {
      expect(experiment.cells[0].levels[name]).toEqual(value);
    }
  });
}

test("ambiguous launch delivery is never automatically retried", async ({ page }) => {
  let launched = 0;
  await page.route("**/api/launches", route => { launched += 1; return route.abort(); });
  await page.goto("/environments/word_guess/play/");
  await page.getByRole("button", { name: "Run episode", exact: true }).click();
  await expect(page.locator("main .notice[role=alert]")).toBeVisible();
  await expect(page.getByRole("link", { name: "View saved run record" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Run episode", exact: true })).toBeDisabled();
  expect(launched).toBe(1);
});
