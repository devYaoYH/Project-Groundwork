import { expect, test, type Locator } from "@playwright/test";
import { canvasId, type WorkflowDocument, type WorkflowGraph } from "../lib/workflow";

async function expectSubgraphSummaries(canvas: Locator, outline: Locator, doc: WorkflowDocument, graph: WorkflowGraph) {
  for (const node of graph.nodes.filter((node) => node.subgraph && (node.kind === "subgraph" || node.kind === "batch"))) {
    const reachable = new Set<string>(), pending = [node.subgraph!];
    while (pending.length) {
      const id = pending.pop()!;
      if (reachable.has(id)) continue;
      reachable.add(id);
      const child = doc.graphs.find((entry) => entry.id === id)!;
      pending.push(...child.nodes.flatMap((entry) => entry.subgraph ? [entry.subgraph] : []));
    }
    const descendants = doc.graphs.filter((entry) => reachable.has(entry.id)).flatMap((entry) => entry.nodes);
    const count = descendants.filter((entry) => entry.kind === "agent_call").length;
    const fanOut = node.kind === "batch" || descendants.some((entry) => entry.kind === "batch");
    const label = `${count} agent call${count === 1 ? "" : "s"}${fanOut ? " \u00b7 fan-out" : ""}`;
    await expect(canvas.locator(`.react-flow__node[data-id="${canvasId(graph.id, node.id)}"] small`)).toHaveText(label);
    const row = outline.getByRole("button", { name: node.id, exact: true }).locator("..");
    await expect(row.getByText(label, { exact: true })).toBeVisible();
  }
}

async function expectClearRoutes(canvas: Locator) {
  const failures = await canvas.evaluate((element) => {
    const nodes = [...element.querySelectorAll<HTMLElement>(".react-flow__node")].map((node) => ({ id: node.dataset.id!, parent: node.dataset.parentId!, box: node.getBoundingClientRect() }));
    const failures: string[] = [];
    const starts = nodes.filter((node) => node.id.endsWith("::__start__"));
    if (!starts.length) failures.push("Missing START box");
    for (const start of starts) for (const node of nodes.filter((node) => node.parent === start.parent)) {
      if (node.id !== start.id && start.box.right >= node.box.left) failures.push(`START is not strictly left of ${node.id}`);
    }
    const ancestorsForEdge = (edgeId: string) => {
      const graphId = edgeId.slice(0, edgeId.lastIndexOf("::"));
      const ancestors = new Set<string>();
      let parent = nodes.find((node) => node.id.startsWith(`${graphId}::`))?.parent;
      while (parent) { ancestors.add(parent); parent = nodes.find((node) => node.id === parent)?.parent; }
      return ancestors;
    };
    for (const handle of element.querySelectorAll<HTMLElement>(".react-flow__handle")) {
      const side = handle.dataset.handleid?.endsWith("source") ? "right" : "left";
      if (!handle.classList.contains(`react-flow__handle-${side}`)) failures.push(`Incorrect ${side} handle orientation`);
    }
    for (const path of element.querySelectorAll<SVGPathElement>(".react-flow__edge-path")) {
      const matrix = path.getScreenCTM()!;
      const tokens = path.getAttribute("d")!.match(/[ML]|-?\d+(?:\.\d+)?(?:e[+-]?\d+)?/gi)!;
      const edgeId = path.closest(".react-flow__edge")!.getAttribute("data-id")!;
      const ancestors = ancestorsForEdge(edgeId);
      for (const [end, offset] of [["source", 0], ["target", tokens.length - 3]] as const) {
        const endpoint = new DOMPoint(Number(tokens[offset + 1]), Number(tokens[offset + 2])).matrixTransform(matrix);
        const handle = element.querySelector<HTMLElement>(`[data-handleid="edge-${edgeId}-${end}"]`)!;
        const box = handle.getBoundingClientRect();
        if (Math.abs(endpoint.x - box.left - box.width / 2) > .5 || Math.abs(endpoint.y - box.top - box.height / 2) > .5) {
          failures.push(`edge ${edgeId} ${end} handle misaligned`);
        }
      }
      let previous: DOMPoint | undefined;
      for (let i = 0; i < tokens.length; i += 3) {
        const point = new DOMPoint(Number(tokens[i + 1]), Number(tokens[i + 2])).matrixTransform(matrix);
        if (tokens[i] === "L" && previous) {
          if (Math.abs(point.x - previous.x) >= .1 && Math.abs(point.y - previous.y) >= .1) failures.push(`${path.id} is not orthogonal`);
          for (const { id, box } of nodes) {
            if (ancestors.has(id)) continue;
            const crosses = Math.abs(point.x - previous.x) < .1
              ? point.x > box.left + .5 && point.x < box.right - .5 && Math.max(point.y, previous.y) > box.top + .5 && Math.min(point.y, previous.y) < box.bottom - .5
              : point.y > box.top + .5 && point.y < box.bottom - .5 && Math.max(point.x, previous.x) > box.left + .5 && Math.min(point.x, previous.x) < box.right - .5;
            if (crosses) failures.push(`${path.id} crosses ${id}`);
          }
        }
        previous = point;
      }
      if (!path.getAttribute("marker-end")) failures.push("Missing arrow marker");
      if (path.closest(".workflow-back-edge") && getComputedStyle(path).strokeDasharray === "none") failures.push("Missing back-edge dashes");
    }
    for (const label of element.querySelectorAll(".react-flow__edge-textbg")) {
      const box = label.getBoundingClientRect();
      const ancestors = ancestorsForEdge(label.closest(".react-flow__edge")!.getAttribute("data-id")!);
      for (const { id, box: node } of nodes) {
        if (ancestors.has(id)) continue;
        if (box.right > node.left + .5 && box.left < node.right - .5 && box.bottom > node.top + .5 && box.top < node.bottom - .5) {
          failures.push(`label overlaps ${id}`);
        }
      }
    }
    return failures;
  });
  expect(failures).toEqual([]);
}

async function expectFittedNodes(canvas: Locator) {
  await expect.poll(() => canvas.evaluate((element) => {
    const bounds = element.getBoundingClientRect();
    return [...element.querySelectorAll(".react-flow__node")].every((node) => {
      const box = node.getBoundingClientRect();
      return box.left >= bounds.left - 1 && box.right <= bounds.right + 1 && box.top >= bounds.top - 1 && box.bottom <= bounds.bottom + 1;
    });
  })).toBeTruthy();
}

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
    await expectClearRoutes(canvas);
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
      await expect(canvas.locator(`.react-flow__node[data-id="${canvasId(graph.id, node.id)}"]`)).toHaveClass(/selected/);
      await expect(inspector.getByText(node.symbol, { exact: true }).first()).toBeVisible();
    }
    await canvas.scrollIntoViewIfNeeded();
    const node = canvas.locator('.react-flow__node[data-id="root::guesser_call"]');
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

test("fetch failures are isolated from detail and workflow", async ({ page, request }) => {
  const { workflow } = await (await request.get("/api/environments/word_guess/workflow")).json();
  await page.route("**/api/environments/word_guess/workflow", (route) => route.fulfill({ status: 503, contentType: "application/json", body: '{"error":"controlled workflow failure"}' }));
  await page.goto("/environments/word_guess/");
  await expect(page.locator(".workflow-section").getByRole("alert")).toContainText("controlled workflow failure");
  await expect(page.getByRole("heading", { name: "Parameters", exact: true })).toBeVisible();
  await page.unroute("**/api/environments/word_guess/workflow");
  await page.route("**/api/environments/word_guess/items?*", (route) => route.abort());
  await page.reload();
  await expect(page.locator(".workflow-canvas .react-flow__node")).toHaveCount(workflow.graphs[0].nodes.length);
});

test("unsupported capability reports unavailable, not a guessed graph", async ({ page }) => {
  await page.route('**/api/environments/negotiation/workflow', route => route.fulfill({
    contentType: 'application/json', body: '{"status":"unavailable","reason":"executable_migration_pending"}'
  }));
  await page.goto("/environments/negotiation/");
  await expect(page.getByText("Executable workflow unavailable: migration pending for this environment.")).toBeVisible();
  await expect(page.locator(".workflow-canvas")).toHaveCount(0);
});

for (const width of [390, 1280]) for (const colorScheme of ["light", "dark"] as const) {
  test(`Negotiation backend/subgraphs at ${width}px ${colorScheme}`, async ({ page, request }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ colorScheme });
    const { status, workflow } = await (await request.get('/api/environments/negotiation/workflow')).json();
    expect(status).toBe('available');
    expect(Object.fromEntries(workflow.graphs.map((graph: WorkflowGraph) => [graph.id, graph.nodes.length]))).toEqual({
      root: 11, 'root/reset_contexts': 8, 'root/reflections': 6,
      'root/round': 14, 'root/round/cheap_talk': 11,
      'root/round/decision_a': 10, 'root/round/decision_b': 10,
    });
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('/environments/negotiation/');
    const section = page.locator('.workflow-section');
    const outline = section.locator('.workflow-outline');
    const canvas = section.locator('.workflow-canvas');
    const inspector = section.getByRole('complementary', { name: 'Step inspector' });
    const inspectGraph = async (id: string) => {
      const graph = workflow.graphs.find((g: { id: string }) => g.id === id);
      await expect(canvas.locator('.react-flow__node')).toHaveCount(graph.nodes.length);
      await expect(canvas.locator('.react-flow__edge')).toHaveCount(graph.edges.length);
      await expectClearRoutes(canvas);
      await expectSubgraphSummaries(canvas, outline, workflow, graph);
      for (const node of graph.nodes) {
        await outline.getByRole('button', { name: node.id, exact: true }).focus();
        await page.keyboard.press('Enter');
        await expect(inspector.getByRole('heading', { name: node.id, exact: true })).toBeVisible();
        await expect(canvas.locator(`.react-flow__node[data-id="${canvasId(graph.id, node.id)}"]`)).toHaveClass(/selected/);
        if (node.actor_selector) await expect(inspector.getByText(node.actor_selector, { exact: true })).toBeVisible();
      }
    };
    const open = async (name: string) => {
      await outline.getByRole('button', { name, exact: true }).focus();
      await page.keyboard.press('Enter');
      await inspector.getByRole('button', { name: 'Open subgraph' }).focus();
      await page.keyboard.press('Enter');
    };
    await inspectGraph('root');
    for (const child of ['reset_contexts', 'reflections']) {
      await open(child);
      await inspectGraph(`root/${child}`);
      for (const key of [child === 'reset_contexts' ? 'reset' : 'reflect', 'bypass']) {
        await expect(canvas.getByText(key, { exact: true }).first()).toBeVisible();
      }
      await section.getByRole('button', { name: 'Parent graph' }).click();
    }
    await open('round');
    await inspectGraph('root/round');
    await open('cheap_talk');
    await inspectGraph('root/round/cheap_talk');
    await expect(outline.getByRole('button', { name: 'next_pass', exact: true })).toBeVisible();
    await expect(outline.getByRole('button', { name: 'talk_call', exact: true })).toBeVisible();
    for (const key of ['both_decided', 'cap', 'bypass_decided']) {
      await expect(canvas.getByText(key, { exact: true }).first()).toBeVisible();
    }
    await section.getByRole('button', { name: 'Parent graph' }).click();
    for (const decision of ['decision_a', 'decision_b']) {
      await open(decision);
      await inspectGraph(`root/round/${decision}`);
      await expect(outline.getByRole('button', { name: 'begin', exact: true })).toBeVisible();
      for (const key of ['bypass_decided', 'retry', 'exhausted', 'valid', 'invalid']) {
        await expect(canvas.getByText(key === 'retry' ? 'retry / back-edge' : key, { exact: true }).first()).toBeVisible();
      }
      await section.getByRole('button', { name: 'Parent graph' }).click();
    }
    await section.getByRole('button', { name: 'Root graph' }).click();
    await expect(outline.getByRole('button', { name: 'complete_game', exact: true })).toBeVisible();
    await expect(section).toContainText('not a replay');
    await expect(canvas.locator('.react-flow__handle:visible')).toHaveCount(0);
    await expect(canvas.locator('input, textarea')).toHaveCount(0);
    await canvas.getByRole('button', { name: 'Fit View', exact: true }).click();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    expect(errors).toEqual([]);
  });
}

test('persisted Negotiation replay remains usable beside lanes/transcript', async ({ page, request }) => {
  const uid = 'workflow-browser-negotiation';
  const detail = await (await request.get(`/api/episodes/${uid}`)).json();
  expect(detail.episode.metrics.num_rounds).toBe(2);
  await page.goto(`/episode/?uid=${uid}`);
  await expect(page.getByRole('heading', { name: 'Transcript', exact: true })).toBeVisible();
  await expect(page.locator('.transcript-line')).toHaveCount(detail.cursor_max);
  const frame = page.frameLocator('iframe[title="negotiation replay"]');
  await expect(frame.locator('#liveGameId')).toHaveText(uid);
  await page.getByRole('button', { name: 'Step back', exact: true }).click();
  await page.getByRole('button', { name: 'Step', exact: true }).click();
  await expect(frame.locator('#liveStatus')).toHaveText(`${detail.cursor_max}/${detail.cursor_max} events`);
  await expect(frame.locator('#liveRounds')).toContainText('Round');
  await expect(frame.locator('#humanInputBar')).toBeHidden();
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
    await expectClearRoutes(canvas);
    for (const route of ["accepted", "rejected", "continue", "finished", "back-edge"]) {
      await expect(canvas.getByText(route, { exact: true })).toBeVisible();
    }
    for (const node of graph.nodes) {
      await outline.getByRole("button", { name: node.id, exact: true }).focus();
      await page.keyboard.press("Enter");
      await expect(inspector.getByRole("heading", { name: node.id, exact: true })).toBeVisible();
      await expect(canvas.locator(`.react-flow__node[data-id="${canvasId(graph.id, node.id)}"]`)).toHaveClass(/selected/);
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
  test(`complete Calendar subgraphs at ${width}px ${colorScheme}`, async ({ page, request }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ colorScheme });
    const { status, workflow } = await (await request.get("/api/environments/calendar/workflow")).json();
    expect(status).toBe("available");
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto("/environments/calendar/");
    const section = page.locator(".workflow-section");
    const inspector = section.getByRole("complementary", { name: "Step inspector" });
    const outline = section.locator(".workflow-outline");
    const canvas = section.locator(".workflow-canvas");
    await expect(section.getByText(/not a complete episode/)).toHaveCount(0);
    const root = workflow.graphs.find((g: { id: string }) => g.id === "root");
    await expect(canvas.locator(".react-flow__node")).toHaveCount(root.nodes.length);
    await expect(canvas.locator('.react-flow__edge')).toHaveCount(root.edges.length);
    await expectClearRoutes(canvas);
    await expectSubgraphSummaries(canvas, outline, workflow, root);
    await expect(canvas.getByText('unavailable', { exact: true })).toHaveCount(0);
    for (const route of ['next', 'reflect', 'done']) {
      await expect(canvas.getByText(route, { exact: true }).first()).toBeVisible();
    }
    for (const node of root.nodes) {
      await outline.getByRole('button', { name: node.id, exact: true }).focus();
      await page.keyboard.press('Enter');
      await expect(inspector.getByRole('heading', { name: node.id, exact: true })).toBeVisible();
      await expect(canvas.locator(`.react-flow__node[data-id="${canvasId(root.id, node.id)}"]`)).toHaveClass(/selected/);
    }
    const openSubgraph = async (name: string) => {
      const select = outline.getByRole("button", { name, exact: true });
      await select.focus(); await page.keyboard.press("Enter");
      const open = inspector.getByRole("button", { name: "Open subgraph" });
      await open.focus(); await page.keyboard.press("Enter");
    };
    await openSubgraph("meeting");
    const meeting = workflow.graphs.find((g: { id: string }) => g.id === "root/meeting");
    await expect(canvas.locator(".react-flow__node")).toHaveCount(meeting.nodes.length);
    await expect(canvas.locator(".react-flow__edge")).toHaveCount(meeting.edges.length);
    await expectClearRoutes(canvas);
    await expectSubgraphSummaries(canvas, outline, workflow, meeting);
    for (const route of ['eligible', 'suppressed', 'success', 'depth_exceeded', 'no_feasible_slot']) {
      await expect(canvas.getByText(route, { exact: true }).first()).toBeVisible();
    }
    for (const node of meeting.nodes) {
      await outline.getByRole("button", { name: node.id, exact: true }).focus();
      await page.keyboard.press("Enter");
      await expect(inspector.getByRole("heading", { name: node.id, exact: true })).toBeVisible();
      await expect(canvas.locator(`.react-flow__node[data-id="${canvasId(meeting.id, node.id)}"]`)).toHaveClass(/selected/);
    }
    for (const name of ["cheap_talk", "voluntary_decisions", "participant_decisions", "round_reflection", "environment_reflection"]) {
      const parentId = name === "environment_reflection" ? "root" : "root/meeting";
      if (parentId === "root") {
        await section.getByRole("button", { name: "Root graph" }).click();
      }
      await openSubgraph(name);
      const graph = workflow.graphs.find((g: { id: string }) => g.id === `${parentId}/${name}`);
      await expect(canvas.locator(".react-flow__node")).toHaveCount(graph.nodes.length);
      await expect(canvas.locator(".react-flow__edge")).toHaveCount(graph.edges.length);
      await expectClearRoutes(canvas);
      await expectSubgraphSummaries(canvas, outline, workflow, graph);
      await expect(section.getByText(/not a complete episode/)).toHaveCount(0);
      for (const node of graph.nodes) {
        const button = outline.getByRole("button", { name: node.id, exact: true });
        await button.focus(); await page.keyboard.press("Enter");
        await expect(inspector.getByRole("heading", { name: node.id, exact: true })).toBeVisible();
        await expect(canvas.locator(`.react-flow__node[data-id="${canvasId(graph.id, node.id)}"]`)).toHaveClass(/selected/);
      }
      if (name.endsWith("decisions")) {
        await expect(canvas.getByText("retry", { exact: true })).toBeVisible();
        expect(graph.nodes.some((n: { kind: string }) => n.kind === "batch")).toBe(false);
        const voluntary = name === "voluntary_decisions";
        await expect(outline.getByRole("button", { name: voluntary ? "apply_voluntary" : "stage_participant", exact: true })).toBeVisible();
        await expect(outline.getByRole("button", { name: voluntary ? "stage_participant" : "apply_voluntary", exact: true })).toHaveCount(0);
        for (const route of ["commit", "rollback"]) {
          await expect(canvas.getByText(route, { exact: true })).toHaveCount(voluntary ? 0 : 1);
        }
      }
      if (name.endsWith("reflection")) {
        await expect(outline.getByRole("button", { name: "launch", exact: true })).toBeVisible();
        await outline.getByRole("button", { name: "bounded_fan_out", exact: true }).click();
        await expect(inspector.getByText(/Bounded concurrent fan-out/)).toBeVisible();
        await inspector.getByRole("button", { name: "Open subgraph" }).focus();
        await page.keyboard.press("Enter");
        await expect(outline.getByRole("button", { name: "reporter_target_call", exact: true })).toBeVisible();
        const child = workflow.graphs.find((g: { id: string }) => g.id === `${graph.id}/bounded_fan_out`);
        await expect(canvas.locator(".react-flow__node")).toHaveCount(child.nodes.length);
        await expect(canvas.locator(".react-flow__edge")).toHaveCount(child.edges.length);
        await expectClearRoutes(canvas);
        await section.getByRole("button", { name: "Parent graph" }).focus(); await page.keyboard.press("Enter");
        await expect(outline.getByRole("button", { name: "join_in_call_order", exact: true })).toBeVisible();
      }
      await section.getByRole("button", { name: "Parent graph" }).focus(); await page.keyboard.press("Enter");
      await expect(outline.getByRole("button", { name: parentId === "root" ? "next_meeting" : "start_meeting", exact: true })).toBeVisible();
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    expect(errors).toEqual([]);
  });
}

for (const environment of ["calendar", "negotiation"]) for (const width of [390, 1280]) for (const colorScheme of ["light", "dark"] as const) {
  test(`${environment} document-based compound expansion at ${width}px ${colorScheme}`, async ({ page, request }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ colorScheme });
    const { workflow: doc }: { workflow: WorkflowDocument } = await (await request.get(`/api/environments/${environment}/workflow`)).json();
    const root = doc.graphs.find((graph) => graph.id === doc.root_graph)!;
    const parent = root.nodes.find((node) => node.subgraph && doc.graphs.find((graph) => graph.id === node.subgraph)!.nodes.some((node) => node.subgraph))!;
    const child = doc.graphs.find((graph) => graph.id === parent.subgraph)!;
    const nested = child.nodes.find((node) => node.subgraph)!;
    const grandchild = doc.graphs.find((graph) => graph.id === nested.subgraph)!;
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`/environments/${environment}/`);
    const section = page.locator(".workflow-section"), canvas = section.locator(".workflow-canvas"), outline = section.locator(".workflow-outline");
    const inspector = section.getByRole("complementary", { name: "Step inspector" });
    const card = (graph: WorkflowGraph, node: string) => canvas.locator(`.react-flow__node[data-id="${canvasId(graph.id, node)}"]`);
    const row = (graph: WorkflowGraph, node: string) => outline.locator(`li[data-canvas-id="${canvasId(graph.id, node)}"]`);
    await expect(canvas.locator(".react-flow__node")).toHaveCount(root.nodes.length);
    await expect(card(root, parent.id).getByRole("button")).toHaveAttribute("aria-expanded", "false");
    await card(root, parent.id).getByRole("button").click();
    await expect(canvas.locator(".react-flow__node")).toHaveCount(root.nodes.length + child.nodes.length);
    await expect(card(root, parent.id)).toHaveClass(/workflow-container/);
    await expectClearRoutes(canvas);
    const expandNested = card(child, nested.id).getByRole("button");
    await card(child, nested.id).focus(); await page.keyboard.press("Tab");
    await expect(expandNested).toBeFocused();
    await page.keyboard.press("Enter");
    const nestedCount = root.nodes.length + child.nodes.length + grandchild.nodes.length;
    await expect(canvas.locator(".react-flow__node")).toHaveCount(nestedCount);
    await expect(card(child, nested.id).getByRole("button")).toHaveAttribute("aria-expanded", "true");
    await expect(card(child, nested.id).getByRole("button")).toBeFocused();
    await expectClearRoutes(canvas);
    await page.keyboard.press("Enter");
    await expect(canvas.locator(".react-flow__node")).toHaveCount(root.nodes.length + child.nodes.length);
    await expect(card(child, nested.id).getByRole("button")).toHaveAttribute("aria-expanded", "false");
    await expect(card(child, nested.id).getByRole("button")).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(canvas.locator(".react-flow__node")).toHaveCount(nestedCount);
    const deepNode = grandchild.nodes.find((node) => node.kind === "agent_call") ?? grandchild.nodes[0];
    await row(grandchild, deepNode.id).getByRole("button", { name: deepNode.id, exact: true }).focus();
    await page.keyboard.press("Enter");
    await expect(inspector.getByRole("heading", { name: deepNode.id, exact: true })).toBeVisible();
    await expect(inspector).toContainText(`Graph: ${grandchild.id}`);
    await expect(card(grandchild, deepNode.id)).toHaveClass(/selected/);
    const target = grandchild.edges.find((edge) => edge.source === deepNode.id)?.target;
    if (target) {
      await inspector.getByRole("button", { name: target, exact: true }).click();
      await expect(card(grandchild, target)).toHaveClass(/selected/);
    }
    await card(root, parent.id).focus(); await page.keyboard.press("Tab");
    await expect(card(root, parent.id).getByRole("button")).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(canvas.locator(".react-flow__node")).toHaveCount(root.nodes.length);
    await expect(card(root, parent.id).getByRole("button")).toHaveAttribute("aria-expanded", "false");
    await expect(card(root, parent.id).getByRole("button")).toBeFocused();
    await expect(inspector.getByRole("heading", { name: "Inspect a step" })).toBeVisible();
    await page.keyboard.press("Enter");
    await expect(canvas.locator(".react-flow__node")).toHaveCount(nestedCount);
    await expect(card(root, parent.id).getByRole("button")).toBeFocused();
    await canvas.getByRole("button", { name: "Fit View", exact: true }).click();
    await expectFittedNodes(canvas);
    await row(child, nested.id).getByRole("button", { name: `Collapse ${nested.id}`, exact: true }).click();
    await expect(canvas.locator(".react-flow__node")).toHaveCount(root.nodes.length + child.nodes.length);
    await expect(card(child, nested.id).getByRole("button")).toHaveAttribute("aria-expanded", "false");
    await row(child, nested.id).getByRole("button", { name: `Expand ${nested.id}`, exact: true }).click();
    await expect(canvas.locator(".react-flow__node")).toHaveCount(nestedCount);
    await row(grandchild, deepNode.id).getByRole("button", { name: deepNode.id, exact: true }).click();
    await section.getByRole("button", { name: "Collapse all", exact: true }).click();
    await expect(canvas.locator(".react-flow__node")).toHaveCount(root.nodes.length);
    await expect(inspector.getByRole("heading", { name: "Inspect a step" })).toBeVisible();
    await expect(outline.locator("li")).toHaveCount(root.nodes.length);
    await section.getByRole("button", { name: "Expand all", exact: true }).click();
    const reachable = new Set<string>(), pending = [root.id];
    while (pending.length) {
      const id = pending.pop()!;
      if (reachable.has(id)) continue;
      reachable.add(id);
      pending.push(...doc.graphs.find((graph) => graph.id === id)!.nodes.flatMap((node) => node.subgraph ? [node.subgraph] : []));
    }
    const graphs = doc.graphs.filter((graph) => reachable.has(graph.id));
    await expect(canvas.locator(".react-flow__node")).toHaveCount(graphs.reduce((sum, graph) => sum + graph.nodes.length, 0));
    await expect(canvas.locator(".react-flow__edge")).toHaveCount(graphs.reduce((sum, graph) => sum + graph.edges.length, 0));
    await expectClearRoutes(canvas);
    const viewport = canvas.locator(".react-flow__viewport"), before = await viewport.getAttribute("style");
    await canvas.getByRole("button", { name: "Zoom In", exact: true }).click();
    await expect(viewport).not.toHaveAttribute("style", before!);
    await canvas.getByRole("button", { name: "Fit View", exact: true }).click();
    await expectFittedNodes(canvas);
    await row(child, nested.id).getByRole("button", { name: nested.id, exact: true }).click();
    await inspector.getByRole("button", { name: "Open subgraph", exact: true }).click();
    await expect(canvas.locator(".react-flow__node")).toHaveCount(grandchild.nodes.length);
    await section.getByRole("button", { name: "Parent graph", exact: true }).click();
    await expect(canvas.locator(".react-flow__node")).toHaveCount(child.nodes.length);
    await expect(card(child, nested.id).getByRole("button")).toHaveAttribute("aria-expanded", "false");
    await section.getByRole("button", { name: "Root graph", exact: true }).click();
    await expect(canvas.locator(".react-flow__node")).toHaveCount(root.nodes.length);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    expect(errors).toEqual([]);
  });
}

for (const malformed of ["cycle", "missing", "duplicate", "shape"]) {
  test(`malformed ${malformed} workflow fails closed without page errors`, async ({ page, request }) => {
    const response = await (await request.get("/api/environments/calendar/workflow")).json();
    const doc: WorkflowDocument = response.workflow;
    const root = doc.graphs.find((graph) => graph.id === doc.root_graph)!;
    if (malformed === "cycle") root.nodes[0].subgraph = root.id;
    if (malformed === "missing") root.nodes[0].subgraph = "missing";
    if (malformed === "duplicate") doc.graphs.push(root);
    if (malformed === "shape") (root as unknown as { nodes: null }).nodes = null;
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/environments/calendar/workflow", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify(response) }));
    await page.goto("/environments/calendar/");
    await expect(page.getByText("Invalid executable workflow. No inferred paths are shown.")).toBeVisible();
    await expect(page.locator(".workflow-canvas")).toHaveCount(0);
    expect(errors).toEqual([]);
  });
}

test("Expand all exceeding the compound budget leaves the collapsed graph inspectable", async ({ page, request }) => {
  const response = await (await request.get("/api/environments/calendar/workflow")).json();
  const doc: WorkflowDocument = response.workflow;
  doc.graphs = [
    { id: "root", nodes: [{ id: "__start__", symbol: "start", kind: "boundary" },
      ...Array.from({ length: 199 }, (_, index) => ({ id: String(index), symbol: "node", kind: "domain" as const, ...(index === 0 ? { subgraph: "child" } : {}) }))], edges: [] },
    { id: "child", nodes: Array.from({ length: 70 }, (_, index) => ({ id: String(index), symbol: "node", kind: "domain" })), edges: [] },
  ];
  await page.route("**/api/environments/calendar/workflow", (route) => route.fulfill({ contentType: "application/json", body: JSON.stringify(response) }));
  await page.goto("/environments/calendar/");
  await expect(page.locator(".workflow-canvas .react-flow__node")).toHaveCount(200);
  await page.locator(".workflow-toolbar").getByRole("button", { name: "Expand all" }).click();
  await expect(page.locator(".workflow-section").getByRole("alert")).toContainText("256-node / 1024-edge");
  await expect(page.locator(".workflow-canvas .react-flow__node")).toHaveCount(200);
  await page.locator(".workflow-outline").getByRole("button", { name: "__start__", exact: true }).click();
  await expect(page.getByRole("complementary", { name: "Step inspector" }).getByRole("heading", { name: "__start__", exact: true })).toBeVisible();
  await page.locator(".workflow-toolbar").getByRole("button", { name: "Collapse all" }).click();
  await expect(page.locator(".workflow-section").getByRole("alert")).toHaveCount(0);
});
