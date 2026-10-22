import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";
import { parse } from "yaml";

type PublishedBinding = {
  type?: string;
  model?: string;
  api_format?: string;
  api_base?: string;
  credential?: string;
  capabilities?: { input?: string[] };
};

// Use the publication manifest as the expectation, independently of the API/UI.
const { agents } = parse(readFileSync(resolve(__dirname, "../../experiments/agents.yaml"), "utf8")) as {
  agents: Record<string, PublishedBinding>;
};
const published = Object.entries(agents).sort(([left], [right]) => left.localeCompare(right));
const textModels = published.filter(([, binding]) => (binding.type ?? "llm") === "llm" &&
  (binding.capabilities?.input ?? ["text"]).includes("text"));

test("the Agents API publishes exactly the shared pool", async ({ request }) => {
  expect(published.length).toBeGreaterThan(0);
  const response = await request.get("/api/agent-pool");
  expect(response.ok()).toBeTruthy();
  const { agents: actual } = await response.json();
  const fields = ({ name, model, api_format, api_base, credential }: {
    name: string; model: string | null; api_format: string | null;
    api_base: string | null; credential: string | null;
  }) => ({ name, model, api_format, api_base, credential });
  expect(actual.map(fields).sort((left: { name: string }, right: { name: string }) => left.name.localeCompare(right.name)))
    .toEqual(published.map(([name, binding]) => ({
      name, model: binding.model ?? null, api_format: binding.api_format ?? null,
      api_base: binding.api_base ?? null, credential: binding.credential ?? null,
    })));
});

for (const [name, binding] of published) {
  test(`${name} is visible on the Agents page with its model and authentication`, async ({ page }) => {
    await page.goto("/agents/");
    const row = page.getByRole("row").filter({ has: page.getByRole("cell", { name, exact: true }) });
    await expect(row).toHaveCount(1);
    await expect(row.getByRole("cell").nth(1)).toHaveText(binding.model ?? "-");
    if (binding.credential === "vertexai_adc" || binding.api_format?.startsWith("vertexai")) {
      await expect(row.getByText("Google ADC", { exact: true })).toBeVisible();
      await expect(row.getByText("none needed", { exact: true })).toHaveCount(0);
    } else if (binding.credential) {
      await expect(row.getByText(binding.credential, { exact: true })).toBeVisible();
    } else {
      await expect(row.getByText("none needed", { exact: true })).toBeVisible();
    }
  });
}

for (const [name, binding] of textModels) {
  test(`${name} can be selected for each text-model seat`, async ({ page }) => {
    await page.goto("/environments/word_guess/play/");
    await page.getByLabel("Run with", { exact: true }).selectOption("live");
    for (const seat of ["Guesser", "Host"]) {
      const picker = page.getByLabel(seat, { exact: true });
      // Check the complete option set too: scripted bindings must not leak in.
      await expect(picker.locator("option")).toHaveCount(textModels.length);
      expect(await picker.locator("option").evaluateAll(options => options.map(option => (option as HTMLOptionElement).value).sort()))
        .toEqual(textModels.map(([bindingName]) => bindingName).sort());
      await picker.selectOption(name);
      await expect(picker).toHaveValue(name);
      await expect(picker.locator("option:checked")).toHaveText(`${binding.model ?? name} · ${name}`);
    }
    // Selecting a roster is provider-free; do not submit a live launch.
  });
}


for (const [environment, seats] of [
  ["tangram_reference", ["Director", "Matcher"]],
  ["map_director", ["Giver", "Follower"]],
] as const) {
  for (const name of ["haiku", "haiku-or", "haiku-5.5-vertex"]) {
    test(`${name} image support makes every ${environment} seat selectable`, async ({ page }) => {
      await page.goto(`/environments/${environment}/play/`);
      await page.getByLabel("Run with", { exact: true }).selectOption("live");
      for (const seat of seats) {
        const picker = page.getByLabel(seat, { exact: true });
        await picker.selectOption(name);
        await expect(picker).toHaveValue(name);
      }
      await expect(page.getByRole("button", { name: "Run episode", exact: true })).toBeEnabled();
    });
  }
}
