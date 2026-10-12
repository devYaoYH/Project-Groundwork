import { defineConfig } from "@playwright/test";

const baseURL = `http://127.0.0.1:${process.env.BROWSER_TEST_PORT ?? "8765"}`;

export default defineConfig({
  testDir: "./tests",
  fullyParallel: false,
  use: { baseURL, trace: "retain-on-failure" },
  webServer: {
    command: "uv run python tests/browser_server.py",
    url: `${baseURL}/api/health`,
    reuseExistingServer: false,
    timeout: 60000,
  },
  projects: [{ name: "chromium", use: { browserName: "chromium" } }],
});
