// Screenshot round for UI-5 (evaluation). Dev server with the mock must run on :5173.
import { chromium } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const out = process.argv[2] ?? "../.impeccable/review/ui5";
mkdirSync(out, { recursive: true });
const base = "http://localhost:5173";
const seeded = "seeded-run";

const goEval = async (p) => {
  await p.getByRole("link", { name: "support-docs" }).first().click();
  await p.locator(".pl-row").first().waitFor();
  const corpus = p.url().split("/corpora/")[1].split("/")[0];
  await p.getByRole("link", { name: "Evaluation" }).click();
  await p.locator(".eval").waitFor();
  return corpus;
};
const openRun = async (p, corpus, run = seeded) => {
  await p.goto(`${base}/corpora/${corpus}/eval/runs/${run}`);
  await p.locator(".ev-table").waitFor({ timeout: 30000 });
  await p.waitForTimeout(600);
};

const shots = [
  { name: "eval-home-dark", scenario: "ready", theme: "dark", tall: true, act: goEval },
  { name: "eval-home-light", scenario: "ready", theme: "light", tall: true, act: goEval },
  { name: "eval-upload-errors-dark", scenario: "ready", theme: "dark", tall: true, act: async (p) => { await goEval(p); await p.getByRole("button", { name: "Upload dataset" }).click(); const f = join(tmpdir(), "bad.jsonl"); writeFileSync(f, '{"question":"q","targets":[{"document":"nope"}]}\n{not json\n'); await p.getByTestId("dataset-input").setInputFiles(f); await p.getByRole("button", { name: "Import dataset" }).click(); await p.locator(".form-error").waitFor(); await p.waitForTimeout(500); } },
  { name: "eval-newrun-dark", scenario: "ready", theme: "dark", tall: true, act: async (p) => { await goEval(p); await p.getByRole("button", { name: "New run" }).click(); await p.waitForTimeout(500); } },
  { name: "run-compare-dark", scenario: "ready", theme: "dark", tall: true, act: async (p) => { const c = await goEval(p); await openRun(p, c); } },
  { name: "run-compare-light", scenario: "ready", theme: "light", tall: true, act: async (p) => { const c = await goEval(p); await openRun(p, c); } },
  { name: "run-running-dark", scenario: "ready", theme: "dark", act: async (p) => {
    const c = await goEval(p);
    const info = await p.evaluate(async (u) => {
      const corpus = document.URL.split("/corpora/")[1].split("/")[0];
      const datasets = await (await fetch(`${u}/v1/corpora/${corpus}/eval/datasets`)).json();
      const pipelines = await (await fetch(`${u}/v1/corpora/${corpus}/pipelines`)).json();
      const ids = pipelines.items.filter((x) => !x.archived).map((x) => x.id);
      const r = await (await fetch(`${u}/v1/eval/runs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dataset_id: datasets.items[0].id, pipeline_ids: [ids[0], ids[1]] }) })).json();
      return r.id;
    }, base);
    await p.goto(`${base}/corpora/${c}/eval/runs/${info}`);
    await p.locator(".ev-progress").waitFor();
    await p.waitForTimeout(700);
  } },
  { name: "item-diff-dark", scenario: "ready", theme: "dark", tall: true, act: async (p) => { const c = await goEval(p); const ids = await p.evaluate(async ({ u, c }) => (await (await fetch(`${u}/v1/corpora/${c}/pipelines`)).json()).items.map((x) => [x.name, x.id]), { u: base, c }); const map = Object.fromEntries(ids); await p.goto(`${base}/corpora/${c}/eval/runs/${seeded}/items?a=${map["baseline-hybrid"]}&b=${map["structure-1200"]}&metric=recall%4010`); await p.locator(".id-item__head").first().waitFor({ timeout: 30000 }); await p.locator(".id-item__head").first().click(); await p.waitForTimeout(500); } },
  { name: "dataset-dark", scenario: "ready", theme: "dark", tall: true, act: async (p) => { const c = await goEval(p); const ds = await p.evaluate(async ({ u, c }) => (await (await fetch(`${u}/v1/corpora/${c}/eval/datasets`)).json()).items[0]?.id, { u: base, c }); if (ds) { await p.goto(`${base}/corpora/${c}/eval/datasets/${ds}`); await p.locator(".ds-table").waitFor(); await p.waitForTimeout(500); } } },
  { name: "run-1024", scenario: "ready", theme: "dark", width: 1024, height: 768, tall: true, act: async (p) => { const c = await goEval(p); await openRun(p, c); } },
  { name: "run-390", scenario: "ready", theme: "dark", width: 390, tall: true, act: async (p) => { const c = await goEval(p); await openRun(p, c); } },
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
