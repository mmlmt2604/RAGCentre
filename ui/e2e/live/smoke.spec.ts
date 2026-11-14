// Shell and serving regressions alongside the five real-backend acceptance flows.
import { expect, test } from "@playwright/test";
import type { Seeded } from "./api";
import { readyCorpus } from "./fixtures";

let seed: Seeded;
const seeded = (): Seeded => seed;
test.beforeAll(async ({ request }, info) => {
  expect((await request.get('/v1/health')).ok()).toBeTruthy();
  const { corpus, pipelines } = await readyCorpus(`smoke-${info.retry}`, true);
  seed = { corpusId: corpus.id, corpusName: corpus.name, pipelines };
});

test.beforeEach(async ({ page }) => {
  page.on("pageerror", (err) => {
    throw err;
  });
});

test("the shell connects to the real API and lists corpora", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("Connected")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Corpora" })).toBeVisible();
  await expect(page.getByRole("link", { name: seeded().corpusName }).first()).toBeVisible();
});

test("a corpus shows its real pipelines with their status", async ({ page }) => {
  const s = seeded();
  await page.goto("/");
  await page.getByRole("link", { name: s.corpusName }).first().click();
  await expect(page).toHaveURL(new RegExp(`/corpora/${s.corpusId}/pipelines`));
  const table = page.getByRole("region", { name: "Pipelines" });
  for (const p of s.pipelines) await expect(table.getByText(p.name, { exact: true })).toBeVisible();
  await expect(table.getByText("Ready").first()).toBeVisible();
});

test("reloading a deep link keeps the page", async ({ page }) => {
  const s = seeded();
  await page.goto(`/corpora/${s.corpusId}/pipelines`);
  await expect(page.getByRole("heading", { name: "Pipelines" })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: "Pipelines" })).toBeVisible();
  await expect(page.getByText(s.pipelines[0].name, { exact: true }).first()).toBeVisible();
});

test("an unknown page is the app's not-found page, not a server 404", async ({ page }) => {
  const response = await page.goto("/no/such/page");
  expect(response?.status()).toBe(200);
  await expect(page.getByText("Nothing at this address")).toBeVisible();
});

test("unknown API paths stay JSON errors", async ({ request }) => {
  const r = await request.get("/v1/no-such-endpoint");
  expect(r.status()).toBe(404);
  expect((await r.json()).error.code).toBe("not_found");
});
