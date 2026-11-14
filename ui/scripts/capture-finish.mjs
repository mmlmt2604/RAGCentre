// Batched UI finishing review. Run against `npm run dev` with the mock API.
import { chromium } from '@playwright/test';
import { mkdirSync, writeFileSync } from 'node:fs';
const out = process.argv[2] ?? '../.impeccable/review/finish';
const base = process.env.RAGCENTRE_CAPTURE_URL ?? 'http://127.0.0.1:5173';
mkdirSync(out, { recursive: true });
const browser = await chromium.launch();
const observations = [];
for (const theme of ['dark', 'light']) {
  for (const width of [1440, 1024, 390]) {
    const context = await browser.newContext({ viewport: { width, height: 900 }, reducedMotion: 'reduce' });
    await context.addInitScript((t) => localStorage.setItem('ragcentre.theme', t), theme);
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await page.goto(`${base}/?scenario=ready`);
    await page.getByRole('link', { name: 'support-docs', exact: true }).first().waitFor();
    const data = await page.evaluate(async () => {
      const get = async (path) => (await fetch(`/v1${path}`)).json();
      const corpus = (await get('/corpora')).items.find((c) => c.name === 'support-docs');
      return {
        corpus, providers: (await get('/providers')).items,
        docs: (await get(`/corpora/${corpus.id}/documents`)).items,
        datasets: (await get(`/corpora/${corpus.id}/eval/datasets`)).items,
      };
    });
    const c = `/corpora/${data.corpus.id}`;
    const a = data.corpus.pipelines.find((p) => p.name === 'baseline-hybrid').id;
    const b = data.corpus.pipelines.find((p) => p.name === 'structure-1200').id;
    const routes = {
      corpora: '/', 'corpus-new': '/corpora/new', providers: '/providers',
      'provider-new': '/providers/new?kind=embedder&type=builtin_hash',
      'provider-detail': `/providers/${data.providers.find((p) => p.kind === 'embedder').id}`,
      documents: `${c}/documents`, 'document-detail': `${c}/documents/${data.docs[0].id}`,
      pipelines: `${c}/pipelines`, builder: `${c}/pipelines/new?from=${a}`,
      'pipeline-detail': `${c}/pipelines/${a}`, diff: `${c}/pipelines/compare?a=${a}&b=${b}`,
      query: `${c}/query?q=warranty&a=${a}&b=${b}`,
      evaluation: `${c}/eval`, dataset: `${c}/eval/datasets/${data.datasets[0].id}`,
      comparison: `${c}/eval/runs/seeded-run`,
      items: `${c}/eval/runs/seeded-run/items?a=${a}&b=${b}&metric=recall%4010`,
      settings: `${c}/settings`, activity: '/activity?status=failed', admin: '/admin',
    };
    for (const [name, path] of Object.entries(routes)) {
      const url = new URL(path, base);
      url.searchParams.set('scenario', name === 'activity' ? 'degraded' : 'ready');
      await page.goto(url.href);
      await page.waitForLoadState('networkidle');
      await page.locator('.rc-skel').first().waitFor({ state: 'hidden', timeout: 10000 }).catch(() => {});
      if (name === 'items') {
        await page.locator('.id-item__head').first().click();
      }
      const file = `${name}-${theme}-${width}.png`;
      await page.screenshot({ path: `${out}/${file}` });
      const issues = await page.evaluate(() => {
        const main = document.querySelector('main');
        return {
          viewportOverflow: document.documentElement.scrollWidth > innerWidth,
          mainOverflow: main ? main.scrollWidth > main.clientWidth + 1 : false,
          unnamedControls: [...document.querySelectorAll('button,input,select,textarea')].filter((e) => {
            if (!e.getClientRects().length || e.getAttribute('type') === 'hidden') return false;
            return !e.getAttribute('aria-label') && !e.getAttribute('aria-labelledby') && !e.textContent.trim() && !e.labels?.length && !e.getAttribute('title');
          }).map((e) => e.outerHTML.slice(0, 160)),
        };
      });
      if (process.env.RAGCENTRE_AXE_PATH && width === 1440) {
        await page.addScriptTag({ path: process.env.RAGCENTRE_AXE_PATH });
        issues.accessibility = await page.evaluate(async () => (await window.axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa', 'wcag22aa'] } })).violations.map((v) => ({ id: v.id, impact: v.impact, nodes: v.nodes.map((n) => ({ target: n.target, summary: n.failureSummary })) })));
      }
      observations.push({ file, ...issues, errors: errors.splice(0) });
      console.log(file, JSON.stringify(issues));
    }
    await context.close();
  }
}
await browser.close();
writeFileSync(`${out}/observations.json`, JSON.stringify(observations, null, 2));
