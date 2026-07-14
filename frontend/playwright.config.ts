import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  retries: 0,
  workers: 1,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: "http://127.0.0.1:18080",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    ...devices["Desktop Chrome"],
  },
  reporter: [["list"]],
  webServer: {
    command: "powershell -NoProfile -Command \"& '../.venv/Scripts/python.exe' '../backend/tests/e2e_server.py'\"",
    url: "http://127.0.0.1:18080/api/health",
    timeout: 120_000,
    reuseExistingServer: false,
  },
});
