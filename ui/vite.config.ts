/// <reference types="vitest/config" />
import { rmSync } from "node:fs";
import { resolve } from "node:path";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";

const outDir = resolve(__dirname, "../src/ragcentre/ui_dist");

// The mock API's service worker lives in public/ for `npm run dev`; the production build never
// starts the mock, so do not ship it.
const dropMockWorker: Plugin = {
  name: "drop-mock-worker",
  apply: "build",
  closeBundle() {
    rmSync(resolve(outDir, "mockServiceWorker.js"), { force: true });
  },
};

export default defineConfig({
  plugins: [react(), dropMockWorker],
  server: {
    port: 5173,
    proxy: { "/v1": "http://127.0.0.1:8700" },
  },
  build: {
    outDir,
    emptyOutDir: true,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    css: { modules: { classNameStrategy: "non-scoped" } },
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
