// Screenshot round for UI-4 (query). Dev server with the mock must run on :5173.
import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";

const out = process.argv[2] ?? "../.impeccable/review/ui4";
mkdirSync(out, { recursive: true });
const base = "http://localhost:5173";
const q = encodeURIComponent("refunds original payment method");

const corpusUrl = async (p) => {
  await p.getByRole("link", { name: "support-docs" }).first().click();
  await p.locator(".pl-row").first().waitFor();
  return `${base}/corpora/${p.url().split("/corpora/")[1].split("/")[0]}`;
};
const pipelineId = async (p, name) => {
  const c = await corpusUrl(p);
  const res = await p.evaluate(async (u) => (await fetch(u)).json(), `/v1/corpora/${c.split("/corpora/")[1]}/pipelines`);
  return { c, id: res.items.find((x) => x.name === name)?.id };
};
const go = (suffix) => async (p) => {
  const c = await corpusUrl(p);
  await p.goto(`${c}/query${suffix}`);
  await p.waitForLoadState("networkidle");
  await p.waitForTimeout(500);
};
const side = (a, b) => async (p) => {
  const { c, id: ia } = await pipelineId(p, a);
  const { id: ib } = await pipelineId(p, b);
  await p.goto(`${c}/query?q=${q}&a=${ia}&b=${ib}`);
  await p.locator(".qres").first().waitFor();
  await p.waitForTimeout(500);
};

const shots = [
  { name: "query-empty-dark", scenario: "ready", theme: "dark", act: go("") },
  { name: "query-single-dark", scenario: "ready", theme: "dark", tall: true, act: go(`?q=${q}`) },
  { name: "query-single-light", scenario: "ready", theme: "light", tall: true, act: go(`?q=${q}`) },
  { name: "query-side-dark", scenario: "ready", theme: "dark", tall: true, act: side("baseline-hybrid", "structure-1200") },
  { name: "query-side-shared-light", scenario: "ready", theme: "light", tall: true, act: side("baseline-hybrid", "rerank-jina") },
  { name: "query-filter-dark", scenario: "ready", theme: "dark", act: async (p) => { await go("")(p); await p.getByRole("button", { name: /^Filter/ }).click(); await p.getByRole("button", { name: "Add condition" }).click(); await p.getByLabel("Condition 1 field").selectOption("year"); await p.getByLabel("Condition 1 operator").selectOption("$gte"); await p.getByLabel("Condition 1 value").fill("2025"); await p.getByRole("button", { name: "Add condition" }).click(); await p.getByLabel("Condition 2 field").selectOption("lang"); await p.getByLabel("Condition 2 value").fill("x y"); } },
  { name: "query-incomplete-dark", scenario: "building", theme: "dark", act: async (p) => { const { c, id } = await pipelineId(p, "structure-1200"); await p.goto(`${c}/query?q=${q}&a=${id}`); await p.locator(".qerr").waitFor(); } },
  { name: "query-1024", scenario: "ready", theme: "dark", width: 1024, height: 768, tall: true, act: side("baseline-hybrid", "structure-1200") },
  { name: "query-390", scenario: "ready", theme: "dark", width: 390, tall: true, act: go(`?q=${q}`) },
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
  await page.waitForTimeout(400);
  await page.screenshot({ path: `${out}/${s.name}.png` });
  console.log(`${s.name}${errors.length ? `  ERRORS: ${errors.join(" | ")}` : ""}`);
  await context.close();
}
await browser.close();
