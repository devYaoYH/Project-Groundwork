import { expect, test } from "@playwright/test";

for (const environment of ["hidden_profile"]) {
  test(`${environment} uses chat turns with preserved round boundaries`, async ({ page, request }) => {
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.setViewportSize({ width: 390, height: 950 });
    const replay = await request.get(`/api/episodes/workflow-browser-${environment}`);
    expect(replay.ok()).toBeTruthy();
    await page.goto(`/episode/?uid=workflow-browser-${environment}`);
    await expect(page.getByRole("heading", { name: "Transcript", exact: true })).toBeVisible();
    const chat = page.getByRole("list", { name: "Episode conversation" });
    await expect(chat.locator(".chat-turn").first()).toBeVisible();
    await expect(chat.locator(".chat-boundary").filter({ hasText: /round start · 2/ })).toHaveCount(1);
    if (environment !== "hidden_profile") {
      const first = chat.locator(".chat-turn").first();
      await expect(first.locator(".chat-input img")).toHaveCount(1);
      await expect(first.locator(".chat-input img")).toHaveAttribute("src", /\/api\/episodes\/workflow-browser-.*\/assets\/[0-9a-f]{64}$/);
      await expect.poll(() => first.locator("img").evaluate(image => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
      expect(await first.evaluate(turn => {
        const input = turn.querySelector(".chat-input");
        const response = turn.querySelector(".chat-message");
        return !!input && !!response && !!(input.compareDocumentPosition(response) & Node.DOCUMENT_POSITION_FOLLOWING);
      })).toBeTruthy();
    } else {
      await expect(chat.getByText("Private response", { exact: true }).first()).toBeVisible();
    }
    await page.getByRole("button", { name: "Show all events", exact: true }).click();
    await expect(page.locator(".transcript-line").first()).toBeVisible();
    await page.getByRole("button", { name: "Show chat", exact: true }).click();
    await expect(chat).toBeVisible();
    const overflow = await page.evaluate(() => ({
      width: window.innerWidth, scrollWidth: document.documentElement.scrollWidth, scrollX: window.scrollX,
      elements: [...document.querySelectorAll("*")]
        .filter(element => element.getBoundingClientRect().right + window.scrollX > window.innerWidth + 2)
        .slice(0, 20)
        .map(element => ({ tag: element.tagName, class: String(element.className), right: element.getBoundingClientRect().right, width: element.getBoundingClientRect().width })),
    }));
    expect(overflow.scrollWidth, JSON.stringify(overflow)).toBeLessThanOrEqual(overflow.width);
    expect(errors).toEqual([]);
  });
}
