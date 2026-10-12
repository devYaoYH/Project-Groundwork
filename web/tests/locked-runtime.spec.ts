import { expect, test } from "@playwright/test";

for (const catalog of ["changed", "removed"]) {
  test(`locked runtime launches retain their frozen plan when the current catalog is ${catalog}`, async ({ page }) => {
    let catalogReads = 0;
    let validations = 0;
    const launchBodies: object[] = [];
    await page.route("**/auth/config", route => route.fulfill({ json: { google_client_id: "", dev_auth: true, csrf_token: "locked-csrf" } }));
    await page.route("**/auth/me", route => route.fulfill({ json: { user: { owner_id: "local:alice", display_name: "Alice", email: "alice@localhost" } } }));
    await page.route("**/api/experiments/pinned-old", route => route.fulfill({ json: {
      experiment: { id: "pinned-old", name: "Locked historical design", environment_id: "old_game", release_id: "old_game@v1",
        locked_at: "2026-10-11T00:00:00Z", design_text: "release: old_game@v1\nparameters:\n  retired_parameter: {pin: true}\n", design_sha256: "original-design" },
      cells: [{ cell_id: "first", levels: {}, episodes_planned: 3 }, { cell_id: "second", levels: {}, episodes_planned: 2 }],
      cell_evidence: [], roster: [], launches: [],
    } }));
    await page.route("**/api/environments/old_game", route => {
      catalogReads += 1;
      return route.fulfill(catalog === "removed" ? { status: 404, json: { error: "Current environment removed" } }
        : { json: { environment_id: "old_game", release: { release_id: "old_game@v2", version: "v2" }, parameters: [], roles: [] } });
    });
    await page.route("**/api/designs/validate", route => {
      validations += 1;
      return route.fulfill({ json: { valid: false, errors: [{ path: "release", message: "Old release is absent from the current catalog" }], plan: null } });
    });
    await page.route("**/api/launches", route => {
      launchBodies.push(route.request().postDataJSON());
      return route.fulfill({ status: 400, json: { error: "The pinned experiment runtime is unavailable; no launch was admitted" } });
    });
    await page.goto("/design/?id=pinned-old");
    await expect(page.getByRole("heading", { name: "Locked historical design", exact: true })).toBeVisible();
    await expect(page.locator("textarea")).toBeDisabled();
    await expect(page.getByText("Locked plan retained from the experiment's runtime.", { exact: true })).toBeVisible();
    await expect(page.locator(".metric").filter({ hasText: "cells" }).locator("strong")).toHaveText("2");
    await expect(page.locator(".metric").filter({ hasText: "episodes" }).locator("strong")).toHaveText("5");
    for (const name of ["Smoke", "Dry run", "Launch live"]) await expect(page.getByRole("button", { name, exact: true })).toBeEnabled();
    await page.getByRole("button", { name: "Smoke", exact: true }).click();
    await expect(page.getByText("The pinned experiment runtime is unavailable; no launch was admitted", { exact: true })).toBeVisible();
    expect(launchBodies).toHaveLength(1);
    expect(launchBodies[0]).toMatchObject({ experiment_id: "pinned-old", mode: "smoke" });
    expect(catalogReads).toBe(0);
    expect(validations).toBe(0);
  });
}
