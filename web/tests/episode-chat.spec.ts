import { expect, test, type Page } from "@playwright/test";

import type { EpisodeDetail, Lane, TraceEvent } from "../lib/api";

const roster: Lane[] = [
  { participant_id: "seller", role: "seller", kind: "scripted", binding: "baseline" },
  { participant_id: "buyer", role: "buyer", kind: "scripted", binding: "baseline" },
];
const dyadEvents: TraceEvent[] = [
  { type: "game_start", data: { units: 3 } },
  { type: "turn_start", synthetic: true, data: { speaker: "seller", round: 1 } },
  { type: "response", data: { speaker: "buyer", text: "I can offer 17.60." } },
  { type: "offer", data: { speaker: "seller", text: "I offer one unit at 18.40.\nCan we agree?", price: 18.4, recorded_context: "x".repeat(240) } },
  { type: "notice", data: { speaker: "system", text: "The round is complete." } },
  { type: "turn_end", synthetic: true, data: { speaker: "buyer" } },
  { type: "offer", data: { speaker: "seller", text: "I offer one unit at 17.60.", price: 17.6 } },
  { type: "game_end", data: { reason: "sold_out", round: 1 } },
];

const negotiationRoster: Lane[] = [
  { participant_id: "first", role: "negotiator", kind: "llm", binding: "fixture" },
  { participant_id: "second", role: "negotiator", kind: "llm", binding: "fixture" },
];
const negotiationEvents: TraceEvent[] = [
  { type: "game_start", data: { num_agents: 2 } },
  { type: "llm_streaming", synthetic: true, data: { agent: "agent_a", round: 1 } },
  { type: "thinking", data: { agent: "agent_a", content: "I value the books most. Open with both books and concede the hats.\nIf B pushes back, trade the ball for one book. " + "longtoken".repeat(30), round: 1 } },
  { type: "reasoning", data: { agent: "agent_b", content: "A is anchoring on books. My valuation favours the ball.\nPropose one book each and keep the ball.", round: 1 } },
  { type: "cheap_talk", data: { speaker: "agent_a", text: "Both books for me; all hats for you?" } },
  { type: "cheap_talk", data: { speaker: "agent_b", text: "One book each, and I take the ball?" } },
  { type: "early_decision", data: { agent: "agent_b", allocation: [1, 0, 1] } },
  { type: "thinking", data: { agent: "agent_b", content: "" } },
  { type: "cheap_talk", data: { agent: "agent_a", text: "An ordinary message attributed through agent." } },
  { type: "cheap_talk", data: { speaker: "system", agent: "agent_b", text: "Cheap talk is over. Submit allocations." } },
  { type: "game_complete", data: { rounds: 1 } },
];

const longParticipant = `carol_${"calendar-participant-".repeat(6)}`;
const calendarRoster: Lane[] = ["alice", "bob", longParticipant, "dan"].map((participant_id) => ({ participant_id, role: "calendar-agent", kind: "llm", binding: "fixture" }));
const calendarEvents: TraceEvent[] = [
  { type: "game_start", data: { num_agents: 4 } },
  { type: "turn_start", synthetic: true, data: { agent_id: 0, round: 1 } },
  { type: "all_groupchat_sent", data: { agent_id: 0, content: "Design review: Tue 10 or Wed 14?", to_agents: [1, 2, 3], channel: "all_groupchat" } },
  { type: "dm_sent", data: { agent_id: 0, content: "Room B is free for either slot.", to_agent: 1, channel: "dm" } },
  { type: "dm_sent", data: { agent_id: 1, content: "Tue 10 works for me.", to_agent: 0, channel: "dm" } },
  { type: "participant_groupchat_sent", data: { agent_id: 2, content: "Tuesday is fine.\nHere is the unbroken reference: " + "calendarreference".repeat(30), to_agents: [0, 1, 0], recorded_context: "long-json-value".repeat(40) } },
  { type: "all_groupchat_sent", data: { agent_id: 3, content: "Could we shift to 11?", to_agents: [2, 0, 1] } },
  { type: "cell_applied", data: { agent_id: 0, slot: "tue-11" } },
  { type: "all_groupchat_sent", data: { agent_id: 0, content: "I applied the slot.", channel: "all_groupchat" } },
  { type: "notice", data: { speaker: "system", text: "One meeting scheduled." } },
  { type: "dm_sent", data: { agent_id: 0, content: "Please confirm.", to_agent: 2 } },
  { type: "thinking", data: { agent_id: 0, content: "I should check everyone's availability.\nThen confirm the meeting." } },
  { type: "dm_sent", data: { agent_id: 0, content: "Thinking complete.", to_agent: 3 } },
  { type: "resolution", data: { round: 1 } },
  { type: "dm_sent", data: { agent_id: 0, content: "Thanks everyone.", to_agents: [1, 2, 3] } },
  { type: "game_end", data: { scheduled: 1 } },
];

function calendarDetail(events = calendarEvents, lanes = calendarRoster) {
  const fixture = detail(events, lanes);
  fixture.episode.config = { environment_id: "calendar", seed: 7 };
  return fixture;
}

async function assertHueAgreement(page: Page, index: number, type: string, laneIndex: number) {
  const row = page.locator(".transcript-line").nth(index);
  const hue = Number(await row.evaluate((element) => element.style.getPropertyValue("--hue")));
  expect(hue).toBe((laneIndex * 79 + 202) % 360);
  const colors = await row.evaluate((element) => {
    const hue = element.style.getPropertyValue("--hue");
    const probe = document.createElement("span");
    element.append(probe);
    probe.style.color = `hsl(${hue} 42% 46%)`;
    const mark = getComputedStyle(probe).color;
    probe.style.color = `hsl(${hue} 42% ${getComputedStyle(element).getPropertyValue("--chat-name-l")})`;
    const name = getComputedStyle(probe).color;
    probe.remove();
    return { mark, name };
  });
  await expect(page.locator(`.lane-mark[title="${index + 1}. ${type}"]`)).toHaveCSS("background-color", colors.mark);
  await expect(row.locator(".chat-meta em")).toHaveCSS("color", colors.name);
  const avatar = row.locator(".chat-avatar");
  if (await avatar.count()) await expect(avatar).toHaveCSS("background-color", colors.mark);
}

function detail(events: TraceEvent[], lanes: Lane[] | null = roster): EpisodeDetail {
  return {
    episode: {
      episode_uid: "chat-fixture",
      config: { environment_id: "buyer_seller", seed: 7 },
      events, final_state: null, metrics: { units_sold: 3 }, release: null,
      episode: null, observability: null, started_at: null, ended_at: null, stopped: false,
    },
    lanes, index_label: null, cursor_max: events.length,
  };
}

function negotiationDetail() {
  const fixture = detail(negotiationEvents, negotiationRoster);
  fixture.episode.config = { environment_id: "negotiation", seed: 7 };
  return fixture;
}

async function openEpisode(page: Page, fixture = detail(dyadEvents)) {
  await page.route("**/api/episodes/chat-fixture", (route) => route.fulfill({ json: fixture }));
  await page.goto("/episode/?uid=chat-fixture");
  await expect(page.getByRole("heading", { name: "Transcript", exact: true })).toBeVisible();
  await expect(page.locator(".transcript-line")).toHaveCount(fixture.cursor_max);
}

const pageErrors = new WeakMap<Page, string[]>();
for (const width of [390, 1280]) {
  test(`Hide system events retains the full Negotiation decision phase at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    const synthetic = ["turn.started", "turn.finished", "cell.started", "cell.finished", "llm.attempt", "llm.request", "llm.response", "llm_streaming", "turn_start", "turn_end", "decide_start", "decide_end", "project_auto_allocation"];
    const semantic: TraceEvent[] = [
      { type: "game_start", data: {} },
      { type: "phase_start", data: { round: 1, phase: "decision" } },
      { type: "thinking", data: { agent: "agent_a", content: "Choose one book and the ball." } },
      { type: "reasoning", data: { agent: "agent_b", content: "Accept the remaining book." } },
      { type: "early_decision", data: { agent: "agent_b", allocation: { books: 1, ball: 0 } } },
      { type: "decision", data: { agent: "agent_a", allocation: { books: 1, ball: 1 } } },
      { type: "submitted", data: { agent: "agent_b", allocation: { books: 1, ball: 0 } } },
      { type: "cell_applied", data: { agent: "agent_a", actions: [{ type: "schedule", slot: 2 }] } },
      { type: "llm.future", data: { content: "Unknown instrumentation-like type stays visible." } },
      { type: "round_complete", data: { round: 1, agent_a_allocation: { books: 1, ball: 1 }, agent_b_allocation: { books: 1, ball: 0 } } },
      { type: "game_complete", data: {} },
    ];
    semantic.push(...synthetic.map((type) => ({ type, data: { synthetic: true } })));
    semantic.push(...[false, null, "true", 1, {}, []].map((synthetic) => ({ type: "llm_streaming", synthetic, data: {} } as unknown as TraceEvent)));
    const fixture = detail([...semantic, ...synthetic.map((type) => ({ type, synthetic: true, data: { agent: "agent_a", text: "Wrapper text", round: 1 } }))], negotiationRoster);
    fixture.episode.config = { environment_id: "negotiation" };
    await openEpisode(page, fixture);
    const rows = page.locator(".transcript-line");
    await rows.nth(2).locator(".chat-thought-text summary").click();
    await rows.nth(5).locator(".chat-payload summary").click();
    await page.getByRole("checkbox", { name: "Hide system events" }).check();
    await expect(page.locator(".transcript-line:visible")).toHaveCount(semantic.length);
    for (let index = 0; index < semantic.length; index++) {
      await expect(rows.nth(index)).toBeVisible();
      await expect(rows.nth(index).locator(".chat-payload pre")).toHaveText(JSON.stringify(semantic[index].data, null, 2));
    }
    for (let index = semantic.length; index < fixture.cursor_max; index++) await expect(rows.nth(index)).toBeHidden();
    await expect(rows.nth(1).locator(".chat-payload pre")).toContainText('"phase": "decision"');
    await expect(rows.nth(2).locator(".chat-thought-text p")).toHaveText("Choose one book and the ball.");
    await rows.nth(3).locator(".chat-thought-text summary").click();
    await expect(rows.nth(3).locator(".chat-thought-text p")).toHaveText("Accept the remaining book.");
    await expect(rows.nth(5).locator(".chat-payload pre")).toBeVisible();
    await page.getByRole("checkbox", { name: "Hide system events" }).uncheck();
    await expect(rows.nth(2).locator(".chat-thought-text")).toHaveAttribute("open", "");
    await expect(rows.nth(5).locator(".chat-payload")).toHaveAttribute("open", "");
  });
}

for (const width of [390, 1280]) {
  test(`Hide system events filters presentation and preserves group boundaries at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await openEpisode(page, calendarDetail());
    const toggle = page.getByRole("checkbox", { name: "Hide system events" });
    const rows = page.locator(".transcript-line");
    const slider = page.getByRole("slider", { name: "Episode cursor" });
    await expect(toggle).not.toBeChecked();
    await rows.nth(11).locator(".chat-thought-text summary").click();
    await rows.nth(11).locator(".chat-payload summary").click();
    await rows.nth(3).locator(".chat-payload summary").click();
    await toggle.focus();
    await page.keyboard.press("Space");
    await expect(toggle).toBeChecked();
    await expect(page.locator(".transcript-line:visible")).toHaveCount(15);
    await expect(page.locator(".chat-thought:visible, .chat-action:visible")).toHaveCount(2);
    await expect(rows.nth(9)).toBeVisible();
    await expect(rows.nth(9)).toHaveClass(/chat-center/);
    await expect(rows.nth(13)).toBeVisible();
    await expect(rows.nth(13)).toHaveClass(/chat-center/);
    await expect(rows.nth(13).locator(".chat-meta")).toContainText("round 1");
    await expect(rows.nth(1)).toBeHidden();
    for (const index of [0, 7, 11, 15]) await expect(rows.nth(index)).toBeVisible();
    await expect(slider).toHaveValue("15");
    await expect(page.locator(".transcript-active:visible")).toHaveCount(1);
    await expect(page.locator(".lane-mark")).toHaveCount(16);
    await expect(rows.nth(3).locator(".chat-payload")).toHaveAttribute("open", "");
    await expect(rows.nth(3)).toHaveClass(/chat-continued/);
    for (const index of [8, 10, 12, 14]) {
      await expect(rows.nth(index)).not.toHaveClass(/chat-continued/);
      await expect(rows.nth(index).locator(".chat-meta em")).not.toHaveClass("visually-hidden");
      await expect(rows.nth(index).locator(".chat-avatar")).toBeVisible();
    }
    await rows.nth(10).locator("p").click();
    await expect(slider).toHaveValue("10");
    await expect(rows.nth(10)).toHaveAttribute("aria-current", "step");
    await page.locator('.lane-mark[title="12. thinking"]').click();
    await expect(slider).toHaveValue("11");
    await expect(page.locator(".transcript-active:visible")).toHaveCount(1);
    await toggle.uncheck();
    await expect(rows.nth(11).locator(".chat-thought-text")).toHaveAttribute("open", "");
    await expect(rows.nth(11).locator(".chat-payload")).toHaveAttribute("open", "");
    await expect(rows.nth(11)).toBeVisible();
    const bounds = await toggle.evaluate((element) => {
      const label = element.parentElement!.getBoundingClientRect();
      const card = element.closest("section")!.getBoundingClientRect();
      return { left: label.left, right: label.right, cardLeft: card.left, cardRight: card.right };
    });
    expect(bounds.left).toBeGreaterThanOrEqual(bounds.cardLeft);
    expect(bounds.right).toBeLessThanOrEqual(bounds.cardRight);
  });
}

test("Hide system events never seeks or pauses playback, including hidden active events", async ({ page }) => {
  await openEpisode(page, negotiationDetail());
  await page.clock.install({ time: new Date("2026-01-01T00:00:00Z") });
  await page.clock.pauseAt(new Date("2026-01-01T00:01:00Z"));
  const slider = page.getByRole("slider", { name: "Episode cursor" });
  const toggle = page.getByRole("checkbox", { name: "Hide system events" });
  await slider.focus();
  await slider.press("Home");
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await toggle.check();
  await expect(slider).toHaveValue("0");
  await expect(page.locator(".transcript-line:visible")).toHaveCount(10);
  await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
  await page.clock.runFor(220);
  await expect(slider).toHaveValue("1");
  await expect(page.locator(".transcript-active:visible")).toHaveCount(0);
  await toggle.uncheck();
  await expect(slider).toHaveValue("1");
  await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
  await toggle.check();
  await page.clock.runFor(660);
  await expect(slider).toHaveValue("4");
  await expect(page.locator(".transcript-active:visible")).toHaveAttribute("aria-label", /Event 5:/);
});

test("Hide system events explains silent and empty attempts without changing lanes", async ({ page }) => {
  await openEpisode(page, detail([
    { type: "llm.request", synthetic: true, data: { speaker: "buyer", text: "Instrumentation text." } },
    { type: "llm_streaming", synthetic: true, data: {} },
    { type: "cell.finished", synthetic: true, data: {} },
  ]));
  await page.getByRole("checkbox", { name: "Hide system events" }).check();
  await expect(page.locator(".transcript-line:visible")).toHaveCount(0);
  await expect(page.getByText("Only synthetic system events were recorded in this attempt. Turn off Hide system events to inspect them.")).toBeVisible();
  await expect(page.getByRole("slider")).toHaveValue("2");
  await page.unroute("**/api/episodes/chat-fixture");
  await openEpisode(page, detail([], null));
  await page.getByRole("checkbox", { name: "Hide system events" }).check();
  await expect(page.getByText("Nothing was said in this attempt.")).toBeVisible();
  await expect(page.getByRole("slider")).toHaveCount(0);
});

for (const event of [
  { type: "notice", data: { text: "No one spoke." } },
  { type: "phase_start", data: { round: "final" } },
] satisfies TraceEvent[]) {
  test(`Hide system events keeps a system-only ${event.type} without a filtered empty state`, async ({ page }) => {
    await openEpisode(page, detail([event, { type: "game_end", data: {} }], null));
    const rows = page.locator(".transcript-line");
    await rows.first().locator(".chat-payload summary").click();
    await page.getByRole("checkbox", { name: "Hide system events" }).check();
    await expect(rows.first()).toBeVisible();
    await expect(rows.last()).toBeVisible();
    await expect(page.getByText(/Turn off Hide system events/)).toHaveCount(0);
    await expect(rows.first().locator(".chat-payload")).toHaveAttribute("open", "");
    await rows.first().focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("slider")).toHaveValue("0");
    await expect(rows.first()).toHaveAttribute("aria-current", "step");
  });
}

test.beforeEach(async ({ page }) => {
  const errors: string[] = [];
  pageErrors.set(page, errors);
  page.on("pageerror", (error) => errors.push(error.message));
});
test.afterEach(async ({ page }) => {
  expect(pageErrors.get(page)).toEqual([]);
  const overflow = await page.evaluate(() => ({
    width: window.innerWidth,
    scrollWidth: document.documentElement.scrollWidth,
    elements: [...document.querySelectorAll("body *")].filter((element) => {
      const rect = element.getBoundingClientRect();
      return rect.right > window.innerWidth && !element.closest(".lane-scroll");
    }).map((element) => ({ class: element.className, right: element.getBoundingClientRect().right })),
  }));
  expect(overflow.scrollWidth, JSON.stringify(overflow)).toBeLessThanOrEqual(overflow.width);
});

test("Negotiation UUID header stays contained at 390px", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 1000 });
  const fixture = negotiationDetail();
  const uid = "ec88273e-728b-4808-b109-921a80b496f3";
  fixture.episode.episode_uid = uid;
  fixture.episode.config = {
    environment_id: "negotiation", seed: 7,
    provenance: { experiment_name: "Negotiation", cell_id: "negotiation-baseline" },
  };
  await openEpisode(page, fixture);
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(uid);
  const breadcrumb = page.getByRole("navigation", { name: "Breadcrumb" });
  await expect(breadcrumb).toHaveCSS("overflow-wrap", "anywhere");
  await expect(breadcrumb.locator(" > span").last()).toHaveText(`/${uid}`);
  const bounds = await breadcrumb.evaluate((element) => {
    const container = element.getBoundingClientRect();
    const label = element.lastElementChild!.getBoundingClientRect();
    return { left: label.left, right: label.right, containerLeft: container.left, containerRight: container.right };
  });
  expect(bounds.left).toBeGreaterThanOrEqual(bounds.containerLeft);
  expect(bounds.right).toBeLessThanOrEqual(bounds.containerRight);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390);
});

for (const width of [390, 1280]) for (const colorScheme of ["light", "dark"] as const) {
  test(`dyad geometry and payloads at ${width}px ${colorScheme}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ colorScheme });
    await openEpisode(page);
    const rows = page.locator(".transcript-line");
    await expect(page.locator("iframe")).toHaveCount(0);
    await expect(page.getByText("no text on this event", { exact: true })).toHaveCount(0);
    await expect(page.locator(".chat-message")).toHaveCount(3);
    await expect(page.locator(".chat-action")).toHaveCount(2);
    await expect(page.locator(".chat-divider")).toHaveCount(2);
    await expect(page.locator(".chat-notice")).toHaveCount(1);
    await expect(rows.nth(2)).toHaveClass(/chat-right/);
    await expect(rows.nth(3)).toHaveClass(/chat-left/);
    await expect(page.locator(".chat-avatar")).toHaveCount(0);
    await assertHueAgreement(page, 2, "response", 1);
    await assertHueAgreement(page, 3, "offer", 0);
    await expect(rows.last()).toHaveClass(/transcript-active/);
    await expect(page.locator(".chat-ahead")).toHaveCount(0);
    const geometry = await page.locator(".chat").evaluate((list) => {
      const card = list.closest("section")!.getBoundingClientRect();
      const lanes = document.querySelector(".lane-scroll")!.closest("section")!.getBoundingClientRect();
      const left = list.querySelector(".chat-left .chat-message")!.getBoundingClientRect();
      const right = list.querySelector(".chat-right .chat-message")!.getBoundingClientRect();
      const notice = list.querySelector(".chat-notice")!.getBoundingClientRect();
      const row = list.querySelector(".chat-center")!.getBoundingClientRect();
      return { cardWidth: card.width, lanesWidth: lanes.width, left: left.left, right: right.right,
        rowLeft: row.left, rowRight: row.right, noticeCenter: (notice.left + notice.right) / 2 };
    });
    expect(geometry.cardWidth).toBeCloseTo(geometry.lanesWidth, 1);
    expect(geometry.left).toBeCloseTo(geometry.rowLeft, 1);
    expect(geometry.right).toBeCloseTo(geometry.rowRight, 1);
    expect(geometry.noticeCenter).toBeCloseTo((geometry.rowLeft + geometry.rowRight) / 2, 1);
    for (const index of [0, 1, 2, 4]) {
      const payload = rows.nth(index).locator("details");
      await payload.locator("summary").click();
      await expect(payload).toHaveAttribute("open", "");
      await expect(payload.locator("pre")).toHaveText(JSON.stringify(dyadEvents[index].data, null, 2));
      await payload.locator("pre").click();
      await expect(rows.last()).toHaveClass(/transcript-active/);
      const content = await payload.locator("pre").boundingBox();
      const meta = await rows.nth(index).locator(".chat-meta").boundingBox();
      expect(content!.y).toBeGreaterThan(meta!.y + meta!.height);
      await payload.locator("summary").focus();
      await page.keyboard.press("Space");
      await expect(payload).not.toHaveAttribute("open");
      await page.keyboard.press("Enter");
      await expect(payload).toHaveAttribute("open", "");
      await expect(page.getByRole("slider", { name: "Episode cursor" })).toHaveValue("7");
      await expect(page.getByRole("button", { name: "Play", exact: true })).toBeVisible();
      await payload.locator("summary").click();
    }
    await page.locator('.lane-mark[title="4. offer"]').click();
    await expect(rows.nth(3)).toHaveClass(/transcript-active/);
    await expect(page.locator(".chat-ahead")).toHaveCount(4);
    await expect(rows.nth(4)).toHaveCSS("opacity", "0.4");
    const screenshot = testInfo.outputPath(`phase-1-dyad-${width}-${colorScheme}.png`);
    await page.screenshot({ path: screenshot, fullPage: true });
    await testInfo.attach(`dyad-${width}-${colorScheme}`, { path: screenshot, contentType: "image/png" });
    await rows.nth(3).locator("summary").click();
    await expect(rows.nth(3).locator("details")).toHaveAttribute("open", "");
    const payloadGeometry = await rows.nth(3).evaluate((row) => {
      const text = row.querySelector("p")!.getBoundingClientRect();
      const pre = row.querySelector("pre")!.getBoundingClientRect();
      const envelope = row.querySelector(".chat-envelope")!.getBoundingClientRect();
      return { textBottom: text.bottom, preTop: pre.top, preRight: pre.right, envelopeRight: envelope.right };
    });
    expect(payloadGeometry.preTop).toBeGreaterThan(payloadGeometry.textBottom);
    expect(payloadGeometry.preRight).toBeLessThanOrEqual(payloadGeometry.envelopeRight);
    const expanded = testInfo.outputPath(`phase-1-payload-${width}-${colorScheme}.png`);
    await page.screenshot({ path: expanded, fullPage: true });
    await testInfo.attach(`payload-${width}-${colorScheme}`, { path: expanded, contentType: "image/png" });
  });
}

test("rows, lane marks, slider and playback share one cursor; disclosures never seek or pause", async ({ page }) => {
  await openEpisode(page);
  await page.clock.install({ time: new Date("2026-01-01T00:00:00Z") });
  await page.clock.pauseAt(new Date("2026-01-01T00:01:00Z"));
  const rows = page.locator(".transcript-line");
  const slider = page.getByRole("slider", { name: "Episode cursor" });
  await rows.nth(2).locator("p").click();
  await expect(slider).toHaveValue("2");
  await expect(rows.nth(2)).toHaveAttribute("aria-current", "step");
  await expect(page.locator(".chat-ahead")).toHaveCount(5);
  await expect(page.locator(".lane-mark-ahead")).toHaveCount(5);
  await rows.nth(1).focus();
  await page.keyboard.press("Enter");
  await expect(slider).toHaveValue("1");
  await rows.nth(3).focus();
  await page.keyboard.press("Space");
  await expect(slider).toHaveValue("3");
  await page.locator('.lane-mark[title="3. response"]').click();
  await expect(slider).toHaveValue("2");
  await slider.focus();
  await slider.press("Home");
  await expect(slider).toHaveValue("0");
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
  for (const index of [1, 2, 4, 7]) {
    const payload = rows.nth(index).locator("details");
    await payload.locator("summary").click();
    await expect(payload).toHaveAttribute("open", "");
    await payload.locator("pre").click();
    await payload.locator("summary").focus();
    await page.keyboard.press("Enter");
    await expect(payload).not.toHaveAttribute("open");
    await page.keyboard.press("Space");
    await expect(payload).toHaveAttribute("open", "");
    await expect(slider).toHaveValue("0");
    await expect(rows.first()).toHaveClass(/transcript-active/);
    await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
  }
  await page.clock.runFor(220);
  await expect(slider).toHaveValue("1");
  await expect(rows.nth(1)).toHaveClass(/transcript-active/);
  await expect(rows.nth(2).locator("details")).toHaveAttribute("open", "");
  await rows.nth(2).locator("p").click();
  await expect(slider).toHaveValue("2");
  await expect(page.getByRole("button", { name: "Play", exact: true })).toBeVisible();
  await page.clock.runFor(660);
  await expect(slider).toHaveValue("2");
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await page.locator('.lane-mark[title="4. offer"]').click();
  await expect(slider).toHaveValue("3");
  await expect(page.getByRole("button", { name: "Play", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await slider.focus();
  await slider.press("Home");
  await expect(slider).toHaveValue("0");
  await expect(page.getByRole("button", { name: "Play", exact: true })).toBeVisible();
  await slider.press("End");
  await page.getByRole("button", { name: "Play", exact: true }).click();
  await page.clock.runFor(220);
  await expect(slider).toHaveValue("7");
  await expect(page.getByRole("button", { name: "Play", exact: true })).toBeVisible();
});

test("system-only and empty episodes render honest states without participant placeholders", async ({ page }) => {
  await openEpisode(page, detail([
    { type: "game_start", data: {} },
    { type: "notice", data: { text: "No participant took a turn." } },
    { type: "game_end", data: { text: "" } },
  ], null));
  await expect(page.locator(".chat-center")).toHaveCount(3);
  await expect(page.locator(".chat-message, .chat-action")).toHaveCount(0);
  await expect(page.locator(".chat-divider")).toHaveCount(2);
  await expect(page.locator(".chat-notice")).toHaveCount(1);
  await expect(page.getByRole("slider")).toHaveValue("2");
  await page.unroute("**/api/episodes/chat-fixture");
  await openEpisode(page, detail([], null));
  await expect(page.locator(".chat > *")).toHaveCount(0);
  await expect(page.getByText("Nothing was said in this attempt.")).toBeVisible();
  await expect(page.getByRole("slider")).toHaveCount(0);
});

test("single speakers stay left and roster-based groups do not invent a right side", async ({ page }) => {
  await openEpisode(page, detail([{ type: "response", data: { speaker: "buyer", text: "Only speaker." } }]));
  await expect(page.locator(".chat-left .chat-message")).toHaveCount(1);
  await page.unroute("**/api/episodes/chat-fixture");
  await openEpisode(page, detail(dyadEvents, [...roster, { participant_id: "observer", kind: "scripted", binding: null }]));
  await expect(page.locator(".chat-group")).toHaveCount(1);
  await expect(page.locator(".chat-left .chat-message")).toHaveCount(3);
  await expect(page.locator(".chat-right")).toHaveCount(0);
  await expect(page.locator(".chat-avatar")).toHaveCount(3);
});

test("distant seeks bring the active row inside the transcript scrollport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 1000 });
  const events = Array.from({ length: 80 }, (_, index) => ({ type: "message", data: {
    speaker: index % 2 ? "buyer" : "seller", text: `Recorded message ${index + 1}.\nA second line of text for scrolling.`,
  } }));
  await openEpisode(page, detail(events));
  const insideScrollport = () => page.locator(".chat").evaluate((list) => {
    const active = list.querySelector(".transcript-active")!.getBoundingClientRect();
    const port = list.getBoundingClientRect();
    return active.top >= port.top && active.bottom <= port.bottom;
  });
  await expect.poll(insideScrollport).toBeTruthy();
  await page.getByRole("checkbox", { name: "Hide system events" }).check();
  await expect.poll(insideScrollport).toBeTruthy();
  await page.locator('.lane-mark[title="2. message"]').click();
  await expect(page.getByRole("slider")).toHaveValue("1");
  await expect.poll(insideScrollport).toBeTruthy();
  await page.locator('.lane-mark[title="71. message"]').click();
  await expect(page.getByRole("slider")).toHaveValue("70");
  await expect.poll(insideScrollport).toBeTruthy();
});

for (const width of [390, 1280]) for (const colorScheme of ["light", "dark"] as const) {
  test(`Negotiation thoughts and ownership at ${width}px ${colorScheme}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ colorScheme });
    await openEpisode(page, negotiationDetail());
    const rows = page.locator(".transcript-line");
    const slider = page.getByRole("slider", { name: "Episode cursor" });
    await expect(page.locator("iframe")).toHaveCount(0);
    await expect(page.getByText("no text on this event", { exact: true })).toHaveCount(0);
    await expect(page.locator(".chat-dyad")).toHaveCount(1);
    await expect(page.locator(".chat-thought")).toHaveCount(2);
    await expect(page.locator(".chat-message")).toHaveCount(3);
    await expect(page.locator(".chat-action")).toHaveCount(3);
    await expect(page.locator(".chat-notice")).toHaveCount(1);
    await expect(page.locator(".chat-divider")).toHaveCount(2);
    await expect(page.locator(".lane-label strong")).toHaveText(["first", "second", "agent_a", "agent_b", "system"]);
    const owners = ["system", "agent_a", "agent_a", "agent_b", "agent_a", "agent_b", "agent_b", "agent_b", "agent_a", "system", "system"];
    for (const [index, owner] of owners.entries()) {
      const row = rows.nth(index);
      await expect(row).toHaveClass(new RegExp(owner === "system" ? "chat-center" : owner === "agent_b" ? "chat-right" : "chat-left"));
      await expect(row).toHaveAttribute("aria-label", new RegExp(`Event ${index + 1}: ${owner},`));
      const mark = page.locator(`.lane-mark[title="${index + 1}. ${negotiationEvents[index].type}"]`);
      const laneIndex = ["first", "second", "agent_a", "agent_b", "system"].indexOf(owner);
      await expect(mark).toHaveCSS("grid-row-start", String(laneIndex + 2));
      const hue = Number(await row.evaluate((element) => element.style.getPropertyValue("--hue")));
      expect(hue).toBe((laneIndex * 79 + 202) % 360);
      const expectedColor = await page.evaluate((hue) => {
        const probe = document.createElement("span");
        probe.style.color = `hsl(${hue} 42% 46%)`;
        document.body.append(probe);
        const color = getComputedStyle(probe).color;
        probe.remove();
        return color;
      }, hue);
      await expect(mark).toHaveCSS("background-color", expectedColor);
    }
    for (const index of [2, 3]) {
      const row = rows.nth(index);
      const thought = row.locator(".chat-thought-text");
      await expect(row.locator(".chat-envelope")).toHaveCSS("border-top-style", "dashed");
      await expect(row.locator(".chat-envelope")).toHaveCSS("background-color", "rgba(0, 0, 0, 0)");
      await expect(thought).not.toHaveAttribute("open");
      await expect(thought.locator("p")).not.toBeVisible();
      await expect(thought.locator("summary")).toContainText(negotiationEvents[index].type!);
      await expect(thought.locator("em")).toHaveText(owners[index]);
      await expect(thought.locator(".chat-thought-preview")).toHaveCSS("white-space", "nowrap");
      await expect(thought.locator(".chat-thought-preview")).toHaveCSS("text-overflow", "ellipsis");
      await expect(thought.locator("summary .chat-payload, summary summary")).toHaveCount(0);
      await row.locator(".chat-payload summary").click();
      await expect(row.locator(".chat-payload")).toHaveAttribute("open", "");
      await expect(row.locator(".chat-payload pre")).toHaveText(JSON.stringify(negotiationEvents[index].data, null, 2));
      await expect(thought).not.toHaveAttribute("open");
      await expect(slider).toHaveValue("10");
      await expect(rows.last()).toHaveClass(/transcript-active/);
      await expect(page.getByRole("button", { name: "Play", exact: true })).toBeVisible();
      await row.locator(".chat-payload summary").click();
    }
    await page.locator('.lane-mark[title="3. thinking"]').click();
    await expect(slider).toHaveValue("2");
    const geometry = await rows.nth(2).evaluate((row) => {
      const left = row.querySelector(".chat-thought")!.getBoundingClientRect();
      const rightRow = row.nextElementSibling!;
      const right = rightRow.querySelector(".chat-thought")!.getBoundingClientRect();
      const bounds = row.getBoundingClientRect();
      const preview = row.querySelector(".chat-thought-preview")!;
      return { left: left.left, right: right.right, rowLeft: bounds.left, rowRight: bounds.right,
        previewWidth: preview.clientWidth, fullWidth: preview.scrollWidth };
    });
    expect(geometry.left).toBeCloseTo(geometry.rowLeft, 1);
    expect(geometry.right).toBeCloseTo(geometry.rowRight, 1);
    expect(geometry.previewWidth).toBeGreaterThan(0);
    expect(geometry.fullWidth).toBeGreaterThan(geometry.previewWidth);
    const collapsed = testInfo.outputPath(`phase-2-thoughts-collapsed-${width}-${colorScheme}.png`);
    await page.screenshot({ path: collapsed, fullPage: true });
    await testInfo.attach(`thoughts-collapsed-${width}-${colorScheme}`, { path: collapsed, contentType: "image/png" });
    for (const index of [2, 3]) {
      const thought = rows.nth(index).locator(".chat-thought-text");
      await thought.locator("summary").click();
      await expect(thought).toHaveAttribute("open", "");
      await expect(thought.locator("p")).toHaveText(String(negotiationEvents[index].data!.content));
      await expect(thought.locator(".chat-thought-preview")).not.toBeVisible();
      await expect(slider).toHaveValue("2");
      await expect(rows.nth(2)).toHaveClass(/transcript-active/);
      await expect(page.getByRole("button", { name: "Play", exact: true })).toBeVisible();
    }
    await rows.nth(2).locator(".chat-payload summary").click();
    const expanded = testInfo.outputPath(`phase-2-thoughts-expanded-${width}-${colorScheme}.png`);
    await page.screenshot({ path: expanded, fullPage: true });
    await testInfo.attach(`thoughts-expanded-${width}-${colorScheme}`, { path: expanded, contentType: "image/png" });
  });
}

test("thought and payload mouse/keyboard interaction preserves cursor, playback and open state", async ({ page }) => {
  await openEpisode(page, negotiationDetail());
  await page.clock.install({ time: new Date("2026-01-01T00:00:00Z") });
  await page.clock.pauseAt(new Date("2026-01-01T00:01:00Z"));
  const rows = page.locator(".transcript-line");
  const slider = page.getByRole("slider", { name: "Episode cursor" });
  await slider.focus();
  await slider.press("Home");
  await page.getByRole("button", { name: "Play", exact: true }).click();
  const unchanged = async () => {
    await expect(slider).toHaveValue("0");
    await expect(rows.first()).toHaveClass(/transcript-active/);
    await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
  };
  for (const index of [2, 3]) {
    const thought = rows.nth(index).locator(".chat-thought-text");
    const payload = rows.nth(index).locator(".chat-payload");
    await payload.locator("summary").click();
    await expect(payload).toHaveAttribute("open", "");
    await expect(thought).not.toHaveAttribute("open");
    await unchanged();
    await payload.locator("pre").click();
    await unchanged();
    await thought.locator("summary").click();
    await expect(thought).toHaveAttribute("open", "");
    await unchanged();
    await thought.locator("p").click();
    await unchanged();
    for (const disclosure of [thought, payload]) {
      await disclosure.locator("summary").focus();
      await page.keyboard.press("Enter");
      await expect(disclosure).not.toHaveAttribute("open");
      await unchanged();
      await page.keyboard.press("Space");
      await expect(disclosure).toHaveAttribute("open", "");
      await unchanged();
    }
  }
  await page.clock.runFor(220);
  await expect(slider).toHaveValue("1");
  await expect(rows.nth(1)).toHaveClass(/transcript-active/);
  await expect(page.getByRole("button", { name: "Pause", exact: true })).toBeVisible();
  await page.locator('.lane-mark[title="6. cheap_talk"]').click();
  await expect(slider).toHaveValue("5");
  await expect(rows.nth(5)).toHaveClass(/transcript-active/);
  await expect(page.getByRole("button", { name: "Play", exact: true })).toBeVisible();
  await rows.first().focus();
  await page.keyboard.press("Space");
  await expect(slider).toHaveValue("0");
  await expect(rows.first()).toHaveClass(/transcript-active/);
  await expect(page.getByRole("button", { name: "Play", exact: true })).toBeVisible();
  for (const index of [2, 3]) {
    await expect(rows.nth(index).locator(".chat-thought-text")).toHaveAttribute("open", "");
    await expect(rows.nth(index).locator(".chat-payload")).toHaveAttribute("open", "");
  }
});

for (const width of [390, 1280]) for (const colorScheme of ["light", "dark"] as const) {
  test(`Calendar group identities and recipients at ${width}px ${colorScheme}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ colorScheme });
    await openEpisode(page, calendarDetail());
    const rows = page.locator(".transcript-line");
    const slider = page.getByRole("slider", { name: "Episode cursor" });
    await expect(page.locator(".chat-group")).toHaveCount(1);
    await expect(page.locator(".chat-right, iframe")).toHaveCount(0);
    await expect(page.locator(".chat-avatar")).toHaveCount(9);
    await expect(page.locator(".chat-continued")).toHaveCount(1);
    await expect(rows.nth(3)).toHaveClass(/chat-continued/);
    await expect(rows.nth(3).locator(".chat-meta em")).toHaveClass("visually-hidden");
    await expect(rows.nth(3)).toHaveAttribute("aria-label", /Event 4: alice, dm_sent/);
    await expect(rows.nth(3).locator(".chat-avatar")).toHaveCSS("visibility", "hidden");
    await expect(rows.nth(2).locator(".chat-avatar")).toHaveText("A");
    await expect(rows.nth(4).locator(".chat-avatar")).toHaveText("B");
    await expect(rows.nth(5).locator(".chat-avatar")).toHaveText("C");
    await expect(rows.nth(6).locator(".chat-avatar")).toHaveText("D");
    const labels: Record<number, string> = { 2: "all", 3: "bob", 4: "alice", 5: "alice, bob", 6: "all", 10: longParticipant, 12: "dan", 14: "all" };
    for (const [index, label] of Object.entries(labels)) {
      await expect(rows.nth(Number(index)).locator(".chat-recipients")).toHaveText(`\u2192 ${label}`);
      await expect(rows.nth(Number(index)).locator(".chat-recipients")).toHaveAttribute("aria-label", `Recipients: ${label}`);
    }
    await expect(rows.nth(8).locator(".chat-recipients")).toHaveCount(0);
    for (const [index, event] of calendarEvents.entries()) {
      const seat = event.data!.agent_id;
      await expect(rows.nth(index)).toHaveClass(typeof seat === "number" ? /chat-left/ : /chat-center/);
      if (typeof seat === "number") await assertHueAgreement(page, index, event.type!, seat);
    }
    const geometry = await rows.nth(3).evaluate((row) => {
      const bounds = row.getBoundingClientRect();
      const bubble = row.querySelector(".chat-envelope")!.getBoundingClientRect();
      const previous = row.previousElementSibling!.querySelector(".chat-envelope")!.getBoundingClientRect();
      const notice = document.querySelector(".chat-notice")!.getBoundingClientRect();
      const avatar = row.querySelector(".chat-avatar")!.getBoundingClientRect();
      return { rowLeft: bounds.left, rowCenter: (bounds.left + bounds.right) / 2, bubbleLeft: bubble.left, previousLeft: previous.left,
        avatarWidth: avatar.width, noticeCenter: (notice.left + notice.right) / 2 };
    });
    expect(geometry.avatarWidth).toBe(22);
    expect(geometry.bubbleLeft).toBeCloseTo(geometry.rowLeft + 30, 1);
    expect(geometry.bubbleLeft).toBeCloseTo(geometry.previousLeft, 1);
    expect(geometry.noticeCenter).toBeCloseTo(geometry.rowCenter, 1);
    await rows.nth(3).locator("p").click();
    await expect(slider).toHaveValue("3");
    await expect(rows.nth(3)).toHaveClass(/transcript-active/);
    await expect(page.locator(".chat-ahead")).toHaveCount(12);
    await rows.nth(2).focus();
    await page.keyboard.press("Enter");
    await expect(slider).toHaveValue("2");
    await rows.nth(3).focus();
    await page.keyboard.press("Space");
    await expect(slider).toHaveValue("3");
    await rows.nth(3).evaluate((element) => element.blur());
    const collapsed = testInfo.outputPath(`phase-3-calendar-${width}-${colorScheme}.png`);
    await page.screenshot({ path: collapsed, fullPage: true });
    await testInfo.attach(`calendar-${width}-${colorScheme}`, { path: collapsed, contentType: "image/png" });
    for (const index of [0, 1, 3, 9, 11]) {
      const payload = rows.nth(index).locator(".chat-payload");
      await payload.locator("summary").click();
      await expect(payload).toHaveAttribute("open", "");
      await expect(payload.locator("pre")).toHaveText(JSON.stringify(calendarEvents[index].data, null, 2));
      await payload.locator("pre").click();
      await expect(slider).toHaveValue("3");
      await expect(rows.nth(3)).toHaveClass(/transcript-active/);
      await payload.locator("summary").click();
    }
    await expect(rows.nth(11).locator(".chat-avatar")).toHaveCount(0);
    await rows.nth(11).locator(".chat-thought-text summary").click();
    await expect(rows.nth(11).locator(".chat-thought-text p")).toHaveText(String(calendarEvents[11].data!.content));
    await expect(slider).toHaveValue("3");
    await rows.nth(5).locator("p").click();
    await rows.nth(5).locator(".chat-payload summary").click();
    const expandedGeometry = await rows.nth(5).evaluate((row) => {
      const bounds = row.getBoundingClientRect();
      const envelope = row.querySelector(".chat-envelope")!.getBoundingClientRect();
      const pre = row.querySelector("pre")!;
      const recipients = document.querySelectorAll(".chat-recipients");
      return { rowRight: bounds.right, envelopeRight: envelope.right, preRight: pre.getBoundingClientRect().right,
        preWidth: pre.clientWidth, preScroll: pre.scrollWidth,
        longTextWrapped: row.querySelector("p")!.getBoundingClientRect().height > 40,
        metadataContained: [...recipients].every((element) => element.getBoundingClientRect().right <= element.closest("li")!.getBoundingClientRect().right) };
    });
    expect(expandedGeometry.envelopeRight).toBeLessThanOrEqual(expandedGeometry.rowRight);
    expect(expandedGeometry.preRight).toBeLessThanOrEqual(expandedGeometry.envelopeRight);
    expect(expandedGeometry.preScroll).toBeGreaterThan(expandedGeometry.preWidth);
    expect(expandedGeometry.longTextWrapped).toBeTruthy();
    expect(expandedGeometry.metadataContained).toBeTruthy();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    await rows.nth(5).locator(".chat-payload pre").evaluate((element) => element.scrollIntoView({ block: "nearest" }));
    const expanded = testInfo.outputPath(`phase-3-calendar-expanded-${width}-${colorScheme}.png`);
    await page.screenshot({ path: expanded, fullPage: true });
    await testInfo.attach(`calendar-expanded-${width}-${colorScheme}`, { path: expanded, contentType: "image/png" });
    await page.unroute("**/api/episodes/chat-fixture");
    const overviewRoster = calendarRoster.map((lane) => ({ ...lane, participant_id: lane.participant_id === longParticipant ? "carol" : lane.participant_id }));
    const overviewEvents = calendarEvents.map((event, index) => index === 5
      ? { ...event, data: { agent_id: 2, content: "Tuesday 10 works for me too.\nRoom A is available.", to_agents: [0, 1] } }
      : event);
    await openEpisode(page, calendarDetail(overviewEvents, overviewRoster));
    await rows.nth(6).locator("p").click();
    await page.locator(".chat").evaluate((element) => { element.scrollTop = 0; });
    const overview = testInfo.outputPath(`phase-3-calendar-overview-${width}-${colorScheme}.png`);
    await page.screenshot({ path: overview, fullPage: true });
    await testInfo.attach(`calendar-overview-${width}-${colorScheme}`, { path: overview, contentType: "image/png" });
  });

  test(`Calendar two-seat recipients remain a dyad at ${width}px ${colorScheme}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ colorScheme });
    await openEpisode(page, calendarDetail([
      { type: "dm_sent", data: { agent_id: 1, content: "Can you meet Tuesday?", to_agent: 0 } },
      { type: "dm_sent", data: { agent_id: 1, content: "How about 10?", to_agent: 0 } },
      { type: "dm_sent", data: { agent_id: 0, content: "Tuesday works.", to_agent: 1 } },
    ], calendarRoster.slice(0, 2)));
    await expect(page.locator(".chat-dyad")).toHaveCount(1);
    await expect(page.locator(".chat-avatar, .chat-continued")).toHaveCount(0);
    await expect(page.locator(".chat-recipients")).toHaveText(["\u2192 all", "\u2192 all", "\u2192 all"]);
    const rows = page.locator(".transcript-line");
    await expect(rows.nth(0)).toHaveClass(/chat-right/);
    await expect(rows.nth(1).locator("em")).toHaveText("bob");
    await expect(rows.nth(2)).toHaveClass(/chat-left/);
    const geometry = await page.locator(".chat").evaluate((list) => {
      const row = list.querySelector("li")!.getBoundingClientRect();
      const right = list.querySelector(".chat-right .chat-envelope")!.getBoundingClientRect();
      const left = list.querySelector(".chat-left .chat-envelope")!.getBoundingClientRect();
      return { rowLeft: row.left, rowRight: row.right, left: left.left, right: right.right };
    });
    expect(geometry.left).toBeCloseTo(geometry.rowLeft, 1);
    expect(geometry.right).toBeCloseTo(geometry.rowRight, 1);
    await assertHueAgreement(page, 0, "dm_sent", 1);
    await assertHueAgreement(page, 2, "dm_sent", 0);
    await page.unroute("**/api/episodes/chat-fixture");
    await openEpisode(page, calendarDetail([
      { type: "dm_sent", data: { agent_id: 1, content: "Only two seats speak.", to_agent: 0 } },
      { type: "dm_sent", data: { agent_id: 0, content: "Four seats are still participating.", to_agent: 1 } },
      { type: "dm_sent", data: { agent_id: 0, content: "Both silent seats stay in the roster.", to_agent: 1 } },
    ]));
    await expect(page.locator(".chat-group")).toHaveCount(1);
    await expect(page.locator(".chat-right")).toHaveCount(0);
    await expect(page.locator(".chat-avatar")).toHaveCount(3);
    await expect(page.locator(".chat-continued")).toHaveCount(1);
    await expect(page.locator(".chat-recipients")).toHaveText(["\u2192 alice", "\u2192 bob", "\u2192 bob"]);
  });
}
