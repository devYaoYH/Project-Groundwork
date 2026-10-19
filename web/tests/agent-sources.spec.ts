import { expect, test } from "@playwright/test";

test("agent bindings expose their configured source and identify ADC authentication", async ({ page, request }) => {
  const pool = await (await request.get("/api/agent-pool")).json();
  const haiku = pool.agents.find((agent: { name: string }) => agent.name === "haiku-5.5-vertex");
  expect(haiku.source).toBe("vertexai");
  expect(pool.agents.some((agent: { name: string }) => ["haiku-5.5", "sonnet-5.5", "opus-5.5"].includes(agent.name))).toBeFalsy();
  await page.goto("/agents/");
  const row = page.getByRole("row").filter({ has: page.getByRole("cell", { name: "haiku-5.5-vertex", exact: true }) });
  await expect(row.getByRole("cell", { name: "vertexai", exact: true })).toBeVisible();
  await expect(row.getByText("Google ADC", { exact: true })).toBeVisible();
  await expect(row.getByText("none needed", { exact: true })).toHaveCount(0);
});
