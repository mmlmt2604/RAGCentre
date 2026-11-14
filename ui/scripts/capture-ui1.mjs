// Screenshot round for UI-1 (providers and corpora). Dev server with the mock must run on :5173.
// Usage: node scripts/capture-ui1.mjs [outDir]
import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";

const out = process.argv[2] ?? "../.impeccable/review/ui1";
mkdirSync(out, { recursive: true });
const base = "http://localhost:5173";

const openProvider = (name) => async (page) => {
  await page.getByRole("link", { name, exact: true }).first().click();
  await page.getByRole("button", { name: "Test connection" }).waitFor();
};
const testIt = async (page) => {
  await page.getByRole("button", { name: "Test connection" }).click();
  await page.locator(".prov-test").waitFor();
};

const shots = [
  { name: "providers-dark", scenario: "ready", path: "/providers", theme: "dark", fullPage: true },
  { name: "providers-light", scenario: "ready", path: "/providers", theme: "light", fullPage: true },
  { name: "providers-empty-dark", scenario: "empty", path: "/providers", theme: "dark", fullPage: true },
  { name: "provider-new-kind-dark", scenario: "ready", path: "/providers/new", theme: "dark" },
  { name: "provider-new-form-dark", scenario: "ready", path: "/providers/new?kind=embedder&type=openai_compatible", theme: "dark", fullPage: true },
  { name: "provider-new-form-light", scenario: "ready", path: "/providers/new?kind=embedder&type=openai_compatible", theme: "light", fullPage: true },
  {
    name: "provider-new-errors-dark", scenario: "empty", path: "/providers/new?kind=parser&type=http", theme: "dark", fullPage: true,
    act: async (page) => {
      await page.getByLabel(/^Name/).fill("Docling");
      await page.getByRole("button", { name: "Connect and create" }).click();
    },
  },
  { name: "provider-detail-fail-dark", scenario: "degraded", path: "/providers", theme: "dark", fullPage: true, act: async (p) => { await openProvider("bge-m3")(p); await testIt(p); } },
  { name: "provider-detail-drift-dark", scenario: "degraded", path: "/providers", theme: "dark", fullPage: true, act: async (p) => { await openProvider("docling")(p); await testIt(p); } },
  { name: "provider-detail-ok-light", scenario: "ready", path: "/providers", theme: "light", fullPage: true, act: async (p) => { await openProvider("qdrant-main")(p); await testIt(p); } },
  {
    name: "provider-delete-dark", scenario: "ready", path: "/providers", theme: "dark",
    act: async (p) => {
      await openProvider("bge-small")(p);
      await p.getByRole("button", { name: "Delete", exact: true }).click();
      await p.getByRole("button", { name: "Delete provider" }).click();
      await p.locator(".rc-dialog__error").waitFor();
    },
  },
  { name: "corpus-new-dark", scenario: "empty", path: "/corpora/new", theme: "dark", act: async (p) => { await p.getByRole("button", { name: "Add field" }).click(); await p.getByRole("button", { name: "Add field" }).click(); await p.getByLabel("New field 1 name").fill("lang"); await p.getByLabel("New field 1 default").fill("en"); await p.getByLabel("New field 2 name").fill("document_id"); await p.getByRole("button", { name: "Create corpus" }).click(); } },
  { name: "corpus-settings-dark", scenario: "ready", path: "/", theme: "dark", fullPage: true, act: async (p) => { await p.getByRole("link", { name: "support-docs" }).first().click(); await p.getByRole("link", { name: "Settings" }).click(); await p.getByRole("button", { name: "Add field" }).click(); } },
  { name: "corpus-settings-light", scenario: "ready", path: "/", theme: "light", fullPage: true, act: async (p) => { await p.getByRole("link", { name: "support-docs" }).first().click(); await p.getByRole("link", { name: "Settings" }).click(); } },
  { name: "providers-1024", scenario: "ready", path: "/providers", theme: "dark", width: 1024, height: 768, fullPage: true },
  { name: "provider-detail-390", scenario: "degraded", path: "/providers", theme: "dark", width: 390, height: 844, fullPage: true, act: async (p) => { await openProvider("bge-m3")(p); await testIt(p); } },
];

const browser = await chromium.launch();
for (const s of shots) {
  const context = await browser.newContext({ viewport: { width: s.width ?? 1440, height: s.fullPage ? 1750 : (s.height ?? 900) }, reducedMotion: "reduce" });
  await context.addInitScript((theme) => localStorage.setItem("ragcentre.theme", theme), s.theme);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await page.goto(`${base}${s.path}${s.path.includes("?") ? "&" : "?"}scenario=${s.scenario}`);
  await page.waitForLoadState("networkidle");
  try {
    if (s.act) await s.act(page);
  } catch (e) {
    errors.push(`act failed: ${e.message.split("\n")[0]}`);
  }
  await page.waitForTimeout(900);
  await page.screenshot({ path: `${out}/${s.name}.png`, fullPage: Boolean(s.fullPage) });
  console.log(`${s.name}${errors.length ? `  ERRORS: ${errors.join(" | ")}` : ""}`);
  await context.close();
}
await browser.close();
