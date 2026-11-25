// Separate real-backend projects preserve legacy acceptance and exercise browser sessions.
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, devices } from "@playwright/test";

const port = Number(process.env.RAGCENTRE_E2E_PORT ?? 8711);
const fixturePort = Number(process.env.RAGCENTRE_E2E_FIXTURE_PORT ?? port + 1000);
const localPort = Number(process.env.RAGCENTRE_E2E_LOCAL_PORT ?? port + 1);
process.env.RAGCENTRE_E2E_FIXTURE_PORT = String(fixturePort);
process.env.RAGCENTRE_E2E_LOCAL_PORT = String(localPort);
export const baseURL = `http://127.0.0.1:${port}`;
const localURL = `http://127.0.0.1:${localPort}`;
const dataDir = process.env.RAGCENTRE_E2E_DATA_DIR ?? mkdtempSync(join(tmpdir(), "ragcentre-e2e-"));
const localDataDir = process.env.RAGCENTRE_E2E_LOCAL_DATA_DIR ?? mkdtempSync(join(tmpdir(), "ragcentre-local-e2e-"));
process.env.RAGCENTRE_E2E_DATA_DIR = dataDir;
process.env.RAGCENTRE_E2E_LOCAL_DATA_DIR = localDataDir;
const root = resolve(fileURLToPath(new URL(".", import.meta.url)), "..");
const commonEnv = {
  RAGCENTRE_HOST: "127.0.0.1", RAGCENTRE_API_TOKEN: "", RAGCENTRE_LOG_LEVEL: "WARNING",
  RAGCENTRE_DISCOVERY_URLS: `http://127.0.0.1:${fixturePort}/ocr-shim`,
};

export default defineConfig({
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  globalSetup: "./e2e/live/global-setup.ts",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 900 },
  },
  projects: [
    { name: "legacy", testDir: "./e2e/live", use: { baseURL } },
    { name: "local-accounts", testDir: "./e2e/local", use: { baseURL: localURL }, timeout: 120_000 },
  ],
  webServer: [{
    command: "node e2e/live/parser-service.mjs",
    url: `http://127.0.0.1:${fixturePort}/health`, reuseExistingServer: false, timeout: 15_000,
    env: { RAGCENTRE_E2E_FIXTURE_PORT: String(fixturePort) },
  }, {
    command: `uv run ragcentre serve --port ${port} --workers 2`, cwd: root,
    url: `${baseURL}/v1/health`, reuseExistingServer: false, timeout: 60_000,
    env: { ...commonEnv, RAGCENTRE_AUTH_MODE: "legacy", RAGCENTRE_DATA_DIR: dataDir },
  }, {
    command: "node e2e/local/server.mjs",
    url: `${localURL}/v1/health`, reuseExistingServer: false, timeout: 60_000,
    env: {
      ...commonEnv, RAGCENTRE_E2E_LOCAL_PORT: String(localPort), RAGCENTRE_DATA_DIR: localDataDir,
      RAGCENTRE_AUTH_MODE: "local", RAGCENTRE_BROWSER_ORIGIN: localURL, RAGCENTRE_INSECURE_LOCAL_COOKIE: "1",
    },
  }],
});
