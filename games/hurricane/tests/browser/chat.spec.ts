import { expect, test } from "../../../../web/tests/support/playwright";

for (const width of [390, 1280]) {
  test(`hurricane preserves group chat, private readings and landfall at ${width}px`, async ({ page, request }) => {
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.setViewportSize({ width, height: 950 });
    const replay = await request.get("/api/episodes/workflow-browser-hurricane");
    expect(replay.ok()).toBeTruthy();
    const detail = await replay.json();
    await page.goto("/episode/?uid=workflow-browser-hurricane");
    await expect(page.getByRole("heading", { name: "Transcript", exact: true })).toBeVisible();
    const chat = page.getByRole("list", { name: "Recorded events", exact: true });
    await expect(chat.locator(".transcript-line")).toHaveCount(detail.cursor_max);
    await expect(page.getByRole("note")).toContainText("Scripted seats");
    await expect(chat).toHaveClass(/chat-group/);
    await expect(chat.getByText("Private response", { exact: true }).first()).toBeVisible();
    await expect(chat.locator(".transcript-line").filter({ has: page.locator("code", { hasText: /^evacuation_order$/ }) }).first()).toBeVisible();
    await expect(chat.locator(".transcript-line").filter({ has: page.locator("code", { hasText: /^landfall$/ }) })).toHaveCount(1);
    await expect(chat).toContainText("Shared data:");
    const cursor = await page.getByRole("slider", { name: "Episode cursor" }).inputValue();
    await page.getByRole("checkbox", { name: "Hide system events" }).check();
    await expect(page.getByRole("slider", { name: "Episode cursor" })).toHaveValue(cursor);
    await expect(page.locator(".transcript-line")).toHaveCount(detail.cursor_max);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    expect(errors).toEqual([]);
  });
}
