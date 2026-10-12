import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.route("**/auth/config", route => route.fulfill({ json: { google_client_id: "", dev_auth: true, csrf_token: "onboarding-csrf" } }));
  await page.route("**/auth/me", route => route.fulfill({ status: 401, json: { error: "Sign in" } }));
});

test("anonymous visitors can copy distinct setup and game-authoring prompts", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Get started with Groundwork" })).toBeVisible();
  const setup = page.getByRole("article").filter({ has: page.getByRole("heading", { name: "Run locally", exact: true }) });
  await setup.getByRole("button", { name: "Copy run locally prompt" }).click();
  await expect(setup.getByRole("status")).toHaveText("Copied to clipboard.");
  const setupClipboard = await page.evaluate(() => navigator.clipboard.readText());
  expect(setupClipboard).toBe((await setup.locator(".installation-prompt-text").innerText()).replace(/^Prompt: /, ""));
  expect(setupClipboard).toContain("skills/groundwork-development/SKILL.md");
  expect(setupClipboard).toContain("docs/INSTALLATION.md");
  await expect(setup.getByRole("status")).toHaveText("Copied to clipboard.");
  const game = page.getByRole("article").filter({ has: page.getByRole("heading", { name: "Author a game", exact: true }) });
  await game.getByRole("button", { name: "Copy author a game prompt" }).click();
  await expect(game.getByRole("status")).toHaveText("Copied to clipboard.");
  const gameClipboard = await page.evaluate(() => navigator.clipboard.readText());
  expect(gameClipboard).toBe((await game.locator(".installation-prompt-text").innerText()).replace(/^Prompt: /, ""));
  expect(gameClipboard).not.toBe(setupClipboard);
  expect(gameClipboard).toContain("docs/ADDING_A_GAME.md");
  await expect(page.getByRole("link", { name: "Contributor instructions", exact: true })).toHaveAttribute("href", "https://github.com/devYaoYH/Project-Groundwork/blob/main/AGENTS.md");
});

test("manual installation supports keyboard tabs and copying runnable commands", async ({ page, context }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/");
  const agentTab = page.getByRole("tab", { name: "Install with a coding agent" });
  await agentTab.focus();
  await page.keyboard.press("ArrowRight");
  const manualTab = page.getByRole("tab", { name: "Manual install", exact: true });
  await expect(manualTab).toBeFocused();
  await expect(manualTab).toHaveAttribute("aria-selected", "true");
  const panel = page.getByRole("tabpanel", { name: "Manual install", exact: true });
  await expect(panel).toBeVisible();
  await panel.getByRole("button", { name: "Copy installation commands" }).click();
  await expect(panel.getByRole("status")).toHaveText("Copied to clipboard.");
  const commands = await page.evaluate(() => navigator.clipboard.readText());
  expect(commands).toBe(await panel.locator("pre").innerText());
  expect(commands).toContain("docker compose -f compose.platform.yml up --build --detach --wait");
  expect(commands).toContain("curl --fail http://127.0.0.1:8080/api/health");
  await page.keyboard.press("Tab");
  await manualTab.focus();
  await page.keyboard.press("Home");
  await expect(agentTab).toBeFocused();
  await expect(agentTab).toHaveAttribute("aria-selected", "true");
});

test("clipboard denial provides selectable complete prompt text", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: {
      writeText: async () => { throw new Error("Clipboard denied"); },
    } });
  });
  await page.goto("/");
  const setup = page.getByRole("article").filter({ has: page.getByRole("heading", { name: "Run locally", exact: true }) });
  await setup.getByRole("button", { name: "Copy run locally prompt" }).click();
  const text = setup.getByRole("textbox", { name: "Copy run locally prompt manually" });
  await expect(text).toBeVisible();
  await expect(text).toBeFocused();
  await expect(text).toHaveValue((await setup.locator(".installation-prompt-text").innerText()).replace(/^Prompt: /, ""));
  expect(await text.evaluate(element => (element as HTMLTextAreaElement).selectionEnd - (element as HTMLTextAreaElement).selectionStart)).toBe((await text.inputValue()).length);
});

test("public onboarding fits a narrow screen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByRole("button", { name: "Copy run locally prompt" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
  await page.getByRole("tab", { name: "Manual install", exact: true }).click();
  await expect(page.getByRole("button", { name: "Copy installation commands" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});
