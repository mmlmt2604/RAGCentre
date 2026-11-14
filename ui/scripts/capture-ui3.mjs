// Screenshot round for UI-3 (pipelines). Dev server with the mock must run on :5173.
import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";

const out = process.argv[2] ?? "../.impeccable/review/ui3";
mkdirSync(out, { recursive: true });
const base = "http://localhost:5173";

const pipelines = async (p) => {
  await p.getByRole("link", { name: "support-docs" }).first().click();
  await p.locator(".pl-row").first().waitFor();
};
const go = (path) => async (p) => {
  await pipelines(p);
  const corpus = p.url().split("/corpora/")[1].split("/")[0];
  await p.goto(`${base}/corpora/${corpus}${path}`);
  await p.waitForLoadState("networkidle");
};
const openPipeline = (name) => async (p) => {
  await pipelines(p);
  await p.getByRole("row", { name: new RegExp(name) }).first().dblclick().catch(() => {});
  await p.locator(".pl-row", { hasText: name }).first().click();
  await p.keyboard.press("Enter");
  await p.getByRole("heading", { name: "Progress" }).waitFor();
  await p.waitForTimeout(600);
};

const shots = [
  { name: "builder-start-dark", scenario: "ready", theme: "dark", act: go("/pipelines/new") },
  { name: "builder-clone-dark", scenario: "ready", theme: "dark", tall: true, act: async (p) => { await go("/pipelines/new")(p); await p.getByLabel("Pipeline", { exact: true }).selectOption({ label: "baseline-hybrid (active)" }); await p.locator(".pg__node", { hasText: "chunk" }).first().click(); await p.getByLabel("Strategy").selectOption("structure"); await p.waitForTimeout(900); } },
  { name: "builder-clone-light", scenario: "ready", theme: "light", tall: true, act: async (p) => { await go("/pipelines/new")(p); await p.getByLabel("Pipeline", { exact: true }).selectOption({ label: "baseline-hybrid (active)" }); await p.waitForTimeout(900); } },
  { name: "builder-problems-dark", scenario: "ready", theme: "dark", tall: true, act: async (p) => { await go("/pipelines/new?template=blank")(p); await p.getByRole("button", { name: "Embed", exact: true }).click(); await p.getByRole("button", { name: "Vector index", exact: true }).click(); await p.waitForTimeout(900); } },
  { name: "builder-yaml-dark", scenario: "ready", theme: "dark", act: async (p) => { await go("/pipelines/new?template=rerank")(p); await p.getByRole("tab", { name: "YAML" }).click(); await p.waitForTimeout(600); } },
  { name: "detail-building-dark", scenario: "building", theme: "dark", tall: true, act: openPipeline("structure-1200") },
  { name: "detail-degraded-dark", scenario: "degraded", theme: "dark", tall: true, act: openPipeline("bge-m3-swap") },
  { name: "detail-ready-light", scenario: "ready", theme: "light", tall: true, act: openPipeline("rerank-jina") },
  { name: "diff-dark", scenario: "ready", theme: "dark", tall: true, act: async (p) => { await openPipeline("baseline-hybrid")(p); await p.getByLabel("Compare with").selectOption({ label: "structure-1200" }); await p.locator(".diff-summary").waitFor(); await p.waitForTimeout(600); } },
  { name: "diff-light", scenario: "ready", theme: "light", tall: true, act: async (p) => { await openPipeline("rerank-jina")(p); await p.getByLabel("Compare with").selectOption({ label: "bge-m3-swap" }); await p.locator(".diff-summary").waitFor(); await p.waitForTimeout(600); } },
  { name: "builder-1024", scenario: "ready", theme: "dark", width: 1024, height: 768, tall: true, act: go("/pipelines/new?template=hybrid") },
  { name: "detail-390", scenario: "degraded", theme: "dark", width: 390, tall: true, act: openPipeline("bge-m3-swap") },
];

const browser = await chromium.launch();
for (const s of shots) {
  const context = await browser.newContext({ viewport: { width: s.width ?? 1440, height: s.tall ? 1750 : (s.height ?? 900) }, reducedMotion: "reduce" });
  await context.addInitScript((theme) => localStorage.setItem("ragcentre.theme", theme), s.theme);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await page.goto(`${base}/?scenario=${s.scenario}`);
  await page.waitForLoadState("networkidle");
  try {
    if (s.act) await s.act(page);
  } catch (e) {
    errors.push(`act failed: ${e.message.split("\n")[0]}`);
  }
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${out}/${s.name}.png` });
  console.log(`${s.name}${errors.length ? `  ERRORS: ${errors.join(" | ")}` : ""}`);
  await context.close();
}
await browser.close();
