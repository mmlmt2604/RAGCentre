// End-to-end tests against a real RAGCentre server (IMPLEMENTATION_PLAN.md M11).
//
//   npm run build        # writes ../src/ragcentre/ui_dist, which the server serves at /
//   npm run e2e
//
// Playwright starts `ragcentre serve` on a fresh, temporary data directory with only the built-in
// zero-setup providers (builtin_text, builtin_hash, sqlite_vec). Nothing is mocked.
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

const port = Number(process.env.RAGCENTRE_E2E_PORT ?? 8711);
const fixturePort = Number(process.env.RAGCENTRE_E2E_FIXTURE_PORT ?? port + 1000);
process.env.RAGCENTRE_E2E_FIXTURE_PORT = String(fixturePort);
export const baseURL = `http://127.0.0.1:${port}`;
const dataDir = process.env.RAGCENTRE_E2E_DATA_DIR ?? mkdtempSync(join(tmpdir(), "ragcentre-e2e-"));
process.env.RAGCENTRE_E2E_DATA_DIR = dataDir; // workers re-evaluate this file; keep one directory

export default defineConfig({
  testDir: "./e2e/live",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  globalSetup: "./e2e/live/global-setup.ts",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL,
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 900 },
  },
  webServer: [{
    command: "node e2e/live/parser-service.mjs",
    url: `http://127.0.0.1:${fixturePort}/health`,
    reuseExistingServer: false,
    timeout: 15_000,
    env: { RAGCENTRE_E2E_FIXTURE_PORT: String(fixturePort) },
  }, {
    command: `uv run ragcentre serve --port ${port} --workers 2`,
    cwd: resolve(fileURLToPath(new URL(".", import.meta.url)), ".."),
    url: `${baseURL}/v1/health`,
    reuseExistingServer: false,
    timeout: 60_000,
    // Only the fixture is discovered, never a shim that happens to run on this machine.
    env: { RAGCENTRE_DISCOVERY_URLS: `http://127.0.0.1:${fixturePort}/ocr-shim`, RAGCENTRE_DATA_DIR: dataDir, RAGCENTRE_LOG_LEVEL: "WARNING" },
  }],
});
