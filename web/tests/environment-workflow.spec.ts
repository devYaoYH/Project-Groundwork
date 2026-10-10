import { expect, test } from "@playwright/test";

for (const width of [390, 1280]) for (const colorScheme of ["light", "dark"] as const) {
  test(`read-only workflow at ${width}px ${colorScheme}`, async ({ page, request }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ colorScheme });
    const response = await request.get("/api/environments/word_guess/workflow");
    const { workflow } = await response.json();
    const graph = workflow.graphs[0];
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto("/environments/word_guess/");
    const canvas = page.locator(".workflow-canvas");
    await expect(canvas.locator(".react-flow__node")).toHaveCount(graph.nodes.length);
    await expect(page.getByRole("heading", { name: "Parameters", exact: true })).toBeVisible();
    await expect(canvas.locator(".react-flow__edge")).toHaveCount(graph.edges.length);
    await expect(canvas.getByText("continue", { exact: true })).toBeVisible();
    await expect(canvas.getByText("finished", { exact: true })).toBeVisible();
    await expect(canvas.getByText("back-edge", { exact: true })).toBeVisible();
    await expect(canvas.locator(".react-flow__attribution")).toBeVisible();
    const outline = page.locator(".workflow-outline");
    const inspector = page.getByRole("complementary", { name: "Step inspector" });
    for (const node of graph.nodes) {
      const button = outline.getByRole("button", { name: node.id, exact: true });
      await button.focus(); await page.keyboard.press("Enter");
      await expect(inspector.getByRole("heading", { name: node.id, exact: true })).toBeVisible();
      await expect(canvas.locator(`.react-flow__node[data-id="${node.id}"]`)).toHaveClass(/selected/);
      await expect(inspector.getByText(node.symbol, { exact: true }).first()).toBeVisible();
    }
    await canvas.scrollIntoViewIfNeeded();
    const node = canvas.locator('.react-flow__node[data-id="guesser_call"]');
    await node.click();
    await expect(outline.getByRole("button", { name: "guesser_call", exact: true })).toHaveAttribute("aria-pressed", "true");
    await expect(node).toBeVisible();
    const position = await node.evaluate((element) => (element as HTMLElement).style.transform);
    const box = await node.boundingBox();
    if (!box) throw new Error("Missing node geometry");
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down(); await page.mouse.move(box.x + 30, box.y + 35, { steps: 5 }); await page.mouse.up();
    expect(await node.evaluate((element) => (element as HTMLElement).style.transform)).toEqual(position);
    await node.focus(); await page.keyboard.press("Delete"); await page.keyboard.press("Backspace");
    await expect(canvas.locator(".react-flow__node")).toHaveCount(graph.nodes.length);
    await expect(canvas.locator(".react-flow__handle:visible")).toHaveCount(0);
    await expect(canvas.locator("input, textarea")).toHaveCount(0);
    const viewport = canvas.locator(".react-flow__viewport");
    const before = await viewport.getAttribute("style");
    await canvas.getByRole("button", { name: "Zoom In", exact: true }).click();
    await expect(viewport).not.toHaveAttribute("style", before!);
    await canvas.getByRole("button", { name: "Fit View", exact: true }).click();
    const fitted = await viewport.getAttribute("style");
    const pane = await canvas.boundingBox();
    if (!pane) throw new Error("Missing canvas geometry");
    await page.mouse.move(pane.x + 10, pane.y + 10); await page.mouse.down();
    await page.mouse.move(pane.x + 50, pane.y + 50, { steps: 5 }); await page.mouse.up();
    await expect(viewport).not.toHaveAttribute("style", fitted!);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    expect(errors).toEqual([]);
  });
}

test("fetch failures are isolated from detail and workflow", async ({ page }) => {
  await page.route("**/api/environments/word_guess/workflow", (route) => route.fulfill({ status: 503, contentType: "application/json", body: '{"error":"controlled workflow failure"}' }));
  await page.goto("/environments/word_guess/");
  await expect(page.locator(".workflow-section").getByRole("alert")).toContainText("controlled workflow failure");
  await expect(page.getByRole("heading", { name: "Parameters", exact: true })).toBeVisible();
  await page.unroute("**/api/environments/word_guess/workflow");
  await page.route("**/api/environments/word_guess/items?*", (route) => route.abort());
  await page.reload();
  await expect(page.locator(".workflow-canvas .react-flow__node")).toHaveCount(10);
});

test("unmigrated environment reports unavailable, not a guessed graph", async ({ page }) => {
  await page.goto("/environments/negotiation/");
  await expect(page.getByText("Executable workflow unavailable: migration pending for this environment.")).toBeVisible();
  await expect(page.locator(".workflow-canvas")).toHaveCount(0);
});

for (const width of [390, 1280]) for (const colorScheme of ["light", "dark"] as const) {
  test(`Buyer-Seller complete workflow at ${width}px ${colorScheme}`, async ({ page, request }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ colorScheme });
    const { status, workflow } = await (await request.get("/api/environments/buyer_seller/workflow")).json();
    expect(status).toBe("available");
    const graph = workflow.graphs[0];
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto("/environments/buyer_seller/");
    const section = page.locator(".workflow-section");
    const canvas = section.locator(".workflow-canvas");
    const outline = section.locator(".workflow-outline");
    const inspector = section.getByRole("complementary", { name: "Step inspector" });
    await expect(canvas.locator(".react-flow__node")).toHaveCount(graph.nodes.length);
    await expect(canvas.locator(".react-flow__edge")).toHaveCount(graph.edges.length);
    for (const route of ["accepted", "rejected", "continue", "finished", "back-edge"]) {
      await expect(canvas.getByText(route, { exact: true })).toBeVisible();
    }
    for (const node of graph.nodes) {
      await outline.getByRole("button", { name: node.id, exact: true }).focus();
      await page.keyboard.press("Enter");
      await expect(inspector.getByRole("heading", { name: node.id, exact: true })).toBeVisible();
      await expect(canvas.locator(`.react-flow__node[data-id="${node.id}"]`)).toHaveClass(/selected/);
      if (node.operation) {
        await expect(inspector.getByText(node.actor_selector, { exact: true })).toBeVisible();
      }
    }
    await expect(section).toContainText("not a replay");
    await expect(canvas.locator(".react-flow__handle:visible")).toHaveCount(0);
    await expect(canvas.locator("input, textarea")).toHaveCount(0);
    await canvas.getByRole("button", { name: "Fit View", exact: true }).click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    expect(errors).toEqual([]);
  });
}

for (const width of [390, 1280]) for (const colorScheme of ["light", "dark"] as const) {
  test(`partial Calendar subgraphs at ${width}px ${colorScheme}`, async ({ page, request }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ colorScheme });
    const { status, workflow } = await (await request.get("/api/environments/calendar/workflow")).json();
    expect(status).toBe("partial");
    await page.goto("/environments/calendar/");
    const section = page.locator(".workflow-section");
    const inspector = section.getByRole("complementary", { name: "Step inspector" });
    const outline = section.locator(".workflow-outline");
    const canvas = section.locator(".workflow-canvas");
    await expect(section.getByText(/not a complete episode/)).toBeVisible();
    const root = workflow.graphs.find((g: { id: string }) => g.id === "root");
    await expect(canvas.locator(".react-flow__node")).toHaveCount(root.nodes.length);
    for (const name of ["cheap_talk_pass", "decision_batch", "reflection_batch"]) {
      const select = outline.getByRole("button", { name, exact: true });
      await select.focus(); await page.keyboard.press("Enter");
      const open = inspector.getByRole("button", { name: "Open subgraph" });
      await open.focus(); await page.keyboard.press("Enter");
      const graph = workflow.graphs.find((g: { id: string }) => g.id === `root/${name}`);
      await expect(canvas.locator(".react-flow__node")).toHaveCount(graph.nodes.length);
      await expect(section.getByText(/not a complete episode/)).toBeVisible();
      for (const node of graph.nodes) {
        const button = outline.getByRole("button", { name: node.id, exact: true });
        await button.focus(); await page.keyboard.press("Enter");
        await expect(inspector.getByRole("heading", { name: node.id, exact: true })).toBeVisible();
        await expect(canvas.locator(`.react-flow__node[data-id="${node.id}"]`)).toHaveClass(/selected/);
      }
      if (name === "decision_batch") {
        await expect(canvas.getByText("retry", { exact: true })).toBeVisible();
        expect(graph.nodes.some((n: { kind: string }) => n.kind === "batch")).toBe(false);
      }
      if (name === "reflection_batch") {
        await outline.getByRole("button", { name: "bounded_fan_out", exact: true }).click();
        await expect(inspector.getByText(/Bounded concurrent fan-out/)).toBeVisible();
        await inspector.getByRole("button", { name: "Open subgraph" }).focus();
        await page.keyboard.press("Enter");
        await expect(outline.getByRole("button", { name: "reporter_target_call", exact: true })).toBeVisible();
        await section.getByRole("button", { name: "Parent graph" }).focus(); await page.keyboard.press("Enter");
        await expect(outline.getByRole("button", { name: "join_reflections", exact: true })).toBeVisible();
      }
      await section.getByRole("button", { name: "Root graph" }).focus(); await page.keyboard.press("Enter");
      await expect(outline.getByRole("button", { name: "driver_boundary", exact: true })).toBeVisible();
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  });
}
