// Screenshot round for UI-2 (documents). Dev server with the mock must run on :5173.
// Usage: node scripts/capture-ui2.mjs [outDir]
import { chromium } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const out = process.argv[2] ?? "../.impeccable/review/ui2";
mkdirSync(out, { recursive: true });
const base = "http://localhost:5173";
const sample = join(tmpdir(), "rc-sample");
mkdirSync(sample, { recursive: true });
const files = ["onboarding-checklist.md", "sso-troubleshooting.md", "release-notes-2025-11.md"].map((n) => {
  const p = join(sample, n);
  writeFileSync(p, `# ${n}\n\nSome text for ${n}.\n`);
  return p;
});

const corpus = async (p, name = "support-docs") => {
  await p.getByRole("link", { name }).first().click();
  await p.getByRole("link", { name: "Documents" }).click();
  await p.locator(".docs-table a").first().waitFor();
};
const openFirst = async (p) => {
  await p.locator(".docs-table a.strong-link").first().click();
  await p.getByRole("heading", { name: "Metadata" }).waitFor();
};

const shots = [
  { name: "docs-dark", scenario: "ready", theme: "dark", act: corpus },
  { name: "docs-light", scenario: "ready", theme: "light", act: corpus },
  { name: "docs-building-dark", scenario: "building", theme: "dark", act: corpus },
  { name: "docs-failed-filter-dark", scenario: "degraded", theme: "dark", act: async (p) => { await corpus(p); await p.getByRole("radio", { name: "Failed" }).click({ force: true }); await p.waitForTimeout(700); } },
  { name: "docs-empty-dark", scenario: "ready", theme: "dark", act: async (p) => { await p.getByRole("link", { name: "eng-wiki" }).first().click(); await p.getByRole("link", { name: "Documents" }).click(); await p.waitForTimeout(600); } },
  { name: "docs-upload-staged-dark", scenario: "ready", theme: "dark", act: async (p) => { await corpus(p); await p.getByTestId("upload-input").setInputFiles(files); await p.getByText("Metadata for these files").click(); } },
  { name: "docs-upload-done-light", scenario: "ready", theme: "light", act: async (p) => { await corpus(p); await p.getByTestId("upload-input").setInputFiles(files); await p.getByRole("button", { name: "Upload 3 files" }).click(); await p.getByRole("button", { name: "Done" }).waitFor(); } },
  { name: "doc-ingestion-failed-dark", scenario: "degraded", theme: "dark", tall: true, act: async (p) => { await corpus(p); await p.getByRole("radio", { name: "Failed" }).click({ force: true }); await p.waitForTimeout(700); await openFirst(p); await p.getByText("provider_error").first().waitFor(); } },
  { name: "doc-ingestion-light", scenario: "ready", theme: "light", tall: true, act: async (p) => { await corpus(p); await openFirst(p); } },
  { name: "doc-parsed-dark", scenario: "ready", theme: "dark", tall: true, act: async (p) => { await corpus(p); await openFirst(p); await p.getByRole("tab", { name: "Parsed" }).click(); await p.locator(".ir-block").first().waitFor(); } },
  { name: "doc-chunks-compare-dark", scenario: "ready", theme: "dark", tall: true, act: async (p) => { await corpus(p); await openFirst(p); await p.getByRole("tab", { name: "Chunks" }).click(); await p.getByLabel("Compare with").selectOption({ label: "structure-1200" }); await p.locator(".chunk").first().waitFor(); await p.waitForTimeout(500); } },
  { name: "doc-meta-edit-dark", scenario: "ready", theme: "dark", act: async (p) => { await corpus(p); await openFirst(p); await p.getByRole("button", { name: "Edit metadata" }).click(); await p.getByLabel(/year/).fill("soon"); await p.getByRole("button", { name: "Save metadata" }).click(); } },
  { name: "docs-1024", scenario: "degraded", theme: "dark", width: 1024, height: 768, act: corpus },
  { name: "doc-detail-390", scenario: "degraded", theme: "dark", width: 390, height: 844, tall: true, act: async (p) => { await p.getByRole("link", { name: "support-docs" }).first().click(); await p.goto(p.url().replace(/\/pipelines.*$/, "/documents")); await p.locator(".docs-table a.strong-link").first().waitFor(); await openFirst(p); } },
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
  await page.waitForTimeout(700);
  await page.screenshot({ path: `${out}/${s.name}.png` });
  console.log(`${s.name}${errors.length ? `  ERRORS: ${errors.join(" | ")}` : ""}`);
  await context.close();
}
await browser.close();
