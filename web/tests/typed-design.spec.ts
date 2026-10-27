import { expect, test } from "@playwright/test";
import { parse, stringify } from "yaml";

test("LLM-only templates save an unbound draft and lock only after model selection", async ({ page, request }) => {
  await page.route("**/api/environments/word_guess", async route => {
    const response = await route.fetch();
    const detail = await response.json();
    await route.fulfill({ response, json: { ...detail,
      roles: detail.roles.map((role: Record<string, unknown>) => ({ ...role, accepts: ["llm"] })) } });
  });
  let launches = 0;
  page.on("request", request => {
    if (request.method() === "POST" && request.url().endsWith("/api/launches")) launches += 1;
  });
  const environments = await (await request.get("/api/environments")).json();
  const environment = environments.environments.find((entry: { environment_id: string }) => entry.environment_id === "word_guess");
  await page.goto("/experiments/");
  await page.getByLabel("Experiment name").fill("Typed model draft");
  await page.getByRole("combobox").selectOption(environment.release_id);
  await page.getByRole("button", { name: "New experiment", exact: true }).click();
  await expect(page).toHaveURL(/\/design\/\?id=/);
  const editor = page.locator("textarea");
  await expect(editor).toHaveValue(/schema_version: 2/);
  const draft = parse(await editor.inputValue());
  expect(draft.roster.every((seat: { kind: string; binding: unknown }) => seat.kind === "llm" && seat.binding === null)).toBeTruthy();
  await expect(page.getByRole("button", { name: "Lock preregistration", exact: true })).toBeDisabled();

  for (const seat of draft.roster) seat.binding = { kind: "agent", name: "gpt-mini" };
  await editor.fill(stringify(draft));
  const lock = page.getByRole("button", { name: "Lock preregistration", exact: true });
  await expect(lock).toBeEnabled();
  await lock.click();
  await expect(page.getByRole("button", { name: "Locked", exact: true })).toBeVisible();
  await expect(editor).toBeDisabled();
  const id = new URL(page.url()).searchParams.get("id");
  const saved = await (await request.get(`/api/experiments/${id}`)).json();
  expect(saved.experiment.locked_at).toBeTruthy();
  expect(saved.roster.every((seat: { kind: string; binding: string }) => seat.kind === "llm" && seat.binding === "gpt-mini")).toBeTruthy();
  expect(launches).toBe(0);
});
