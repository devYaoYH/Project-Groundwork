import { expect, test } from "../../../../web/tests/support/playwright";

for (const environment of ["map_director", "map_director-v2"]) for (const width of [390, 1280]) {
  test(`${environment} preserves common chat and final outputs at ${width}px`, async ({ page, request }) => {
    const errors: string[] = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.setViewportSize({ width, height: 950 });
    const replay = await request.get(`/api/episodes/workflow-browser-${environment}`);
    expect(replay.ok()).toBeTruthy();
    const detail = await replay.json();
    await page.goto(`/episode/?uid=workflow-browser-${environment}`);
    await expect(page.getByRole("heading", { name: "Transcript", exact: true })).toBeVisible();
    const chat = page.getByRole("list", { name: "Recorded events", exact: true });
    await expect(chat.locator(".transcript-line")).toHaveCount(detail.cursor_max);
    await expect(page.getByRole("note")).toContainText("Scripted seats");
    await expect(chat.locator(".chat-input").first()).toBeVisible();
    if (environment !== "hidden_profile") {
      await expect(chat).toHaveClass(/chat-dyad/);
      const input = chat.locator(".chat-input").first();
      await expect(input.locator("img")).toHaveCount(1);
      expect(await input.evaluate(element => {
        const image = element.querySelector(".media-preview");
        const text = element.querySelector(".chat-input-text");
        return !!image && !!text && !!(image.compareDocumentPosition(text) & Node.DOCUMENT_POSITION_FOLLOWING);
      })).toBeTruthy();
      const type = environment === "tangram_reference" ? "placement" : "drawing";
      const outputs = chat.locator(".transcript-line").filter({ has: page.locator(`code`, { hasText: new RegExp(`^${type}$`) }) });
      await expect(outputs).toHaveCount(2);
      await expect(outputs.locator(".chat-action img")).toHaveCount(2);
      await expect.poll(() => outputs.locator("img").first().evaluate(image => (image as HTMLImageElement).naturalWidth)).toBeGreaterThan(0);
      await expect(outputs.first()).toContainText(/submitted/i);
      await expect(chat.locator(".transcript-line").filter({ has: page.locator("code", { hasText: /^round_end$/ }) }).locator("img")).toHaveCount(2);
      if (environment === "tangram_reference") await expect(chat).not.toContainText("Visual signatures");
    } else {
      await expect(chat.getByText("Private response", { exact: true }).first()).toBeVisible();
      await expect(chat).toHaveClass(/chat-group/);
    }
    const cursor = await page.getByRole("slider", { name: "Episode cursor" }).inputValue();
    await page.getByRole("checkbox", { name: "Hide system events" }).check();
    await expect(page.getByRole("slider", { name: "Episode cursor" })).toHaveValue(cursor);
    await expect(page.locator(".transcript-line")).toHaveCount(detail.cursor_max);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);
    expect(errors).toEqual([]);
  });
}
