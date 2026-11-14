// Screenshot round for UI-6 (Activity and Admin). Dev server with the mock must run on :5173.
import { chromium } from "@playwright/test";
import { mkdirSync } from "node:fs";

const out = process.argv[2] ?? "../.impeccable/review/ui6";
mkdirSync(out, { recursive: true });
const base = "http://localhost:5173";

const shots = [
  { name: "activity-degraded-dark", scenario: "degraded", path: "/activity", theme: "dark", tall: true },
  { name: "activity-light", scenario: "ready", path: "/activity", theme: "light", tall: true },
  { name: "activity-failed-filter-dark", scenario: "degraded", path: "/activity?status=failed", theme: "dark", tall: true },
  { name: "admin-dark", scenario: "ready", path: "/admin", theme: "dark" },
  { name: "admin-light", scenario: "ready", path: "/admin", theme: "light" },
  { name: "admin-gc-dark", scenario: "degraded", path: "/admin", theme: "dark", act: async (p) => { await p.getByRole("button", { name: "Preview" }).click(); await p.locator(".gc-report").waitFor(); } },
  { name: "activity-390", scenario: "degraded", path: "/activity?status=failed", theme: "dark", width: 390, tall: true },
];

const browser = await chromium.launch();
for (const s of shots) {
  const context = await browser.newContext({ viewport: { width: s.width ?? 1440, height: s.tall ? 1750 : (s.height ?? 900) }, reducedMotion: "reduce" });
  await context.addInitScript((theme) => localStorage.setItem("ragcentre.theme", theme), s.theme);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  await page.goto(`${base}${s.path}?scenario=${s.scenario}`);
  await page.waitForLoadState("networkidle");
  try { if (s.act) await s.act(page); } catch (e) { errors.push(`act: ${e.message.split("\n")[0]}`); }
  await page.waitForTimeout(500);
  await page.screenshot({ path: `${out}/${s.name}.png` });
  console.log(`${s.name}${errors.length ? `  ERRORS: ${errors.join(" | ")}` : ""}`);
  await context.close();
}
await browser.close();
