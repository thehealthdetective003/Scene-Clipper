import { defineConfig, devices } from "@playwright/test";

const browserChannel = process.env.E2E_BROWSER_CHANNEL === "chrome" ? "chrome" : undefined;

/**
 * Browser-level end-to-end coverage (spec 12.7).
 *
 * These run against a live stack: start the API, a worker, and the dev server
 * first (see README), or point E2E_BASE_URL at a running deployment. Gemini is
 * mocked at the backend, so no credential is ever needed here.
 */
export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:5173",
    trace: "retain-on-failure",
    // Never record credentials into a trace or video.
    video: "off",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        ...(browserChannel ? { channel: browserChannel } : {}),
      },
    },
  ],
});
