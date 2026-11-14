// Screenshot round for the impeccable milestone check (IMPLEMENTATION_PLAN.md §21.1).
// Usage: node scripts/capture.mjs [outDir]   (dev server must be running on :5173 with the mock)
import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";

const out = process.argv[2] ?? "../.impeccable/review";
mkdirSync(out, { recursive: true });
const base = "http://localhost:5173";

const shots = [
  { name: "pipelines-degraded-dark", scenario: "degraded", path: "/", corpus: true, theme: "dark" },
  { name: "pipelines-degraded-light", scenario: "degraded", path: "/", corpus: true, theme: "light" },
  { name: "pipelines-building-dark", scenario: "building", path: "/", corpus: true, theme: "dark" },
  { name: "pipelines-ready-dark", scenario: "ready", path: "/", corpus: true, theme: "dark" },
  { name: "corpora-ready-dark", scenario: "ready", path: "/", theme: "dark" },
  { name: "corpora-empty-dark", scenario: "empty", path: "/", theme: "dark" },
  { name: "auth-prompt-dark", scenario: "auth", path: "/", theme: "dark" },
  { name: "not-found-dark", scenario: "ready", path: "/nowhere", theme: "dark" },
  { name: "network-fail-dark", scenario: "ready", path: "/", corpus: true, theme: "dark", failNetwork: true },
  { name: "pipelines-degraded-1024", scenario: "degraded", path: "/", corpus: true, theme: "dark", width: 1024, height: 768 },
  { name: "pipelines-degraded-390", scenario: "degraded", path: "/", corpus: true, theme: "dark", width: 390, height: 844, fullPage: true },
];

const browser = await chromium.launch();
for (const s of shots) {
  const context = await browser.newContext({ viewport: { width: s.width ?? 1440, height: s.height ?? 900 }, reducedMotion: "reduce" });
  await context.addInitScript((theme) => localStorage.setItem("ragcentre.theme", theme), s.theme);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(`${base}${s.path}?scenario=${s.scenario}`);
  await page.waitForLoadState("networkidle");
  if (s.corpus) {
    await page.getByRole("link", { name: "support-docs" }).first().click();
    await page.waitForSelector(".pl-row", { timeout: 10000 });
  }
  if (s.failNetwork) {
    await page.getByLabel("Fail requests").check();
    await page.waitForTimeout(2500);
  }
  await page.waitForTimeout(1200);
  await page.screenshot({ path: `${out}/${s.name}.png`, fullPage: Boolean(s.fullPage) });
  console.log(`${s.name}${errors.length ? `  ERRORS: ${errors.join(" | ")}` : ""}`);
  await context.close();
}
await browser.close();
