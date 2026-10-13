import { defineConfig } from "@playwright/test";

const port = process.env.GROUNDWORK_BROWSER_PORT ?? "8765";

export default defineConfig({
  testDir: "..",
  testMatch: ["web/tests/**/*.spec.ts", "games/*/tests/browser/**/*.spec.ts"],
  fullyParallel: false,
  use: { baseURL: `http://127.0.0.1:${port}`, trace: "retain-on-failure" },
  webServer: {
    command: "uv run python tests/browser_server.py",
    url: `http://127.0.0.1:${port}/api/health`,
    reuseExistingServer: false,
    timeout: 60000,
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
