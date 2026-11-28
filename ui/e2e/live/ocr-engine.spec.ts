import { expect, test } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import type { Pipeline, Provider, QueryResponse } from '../../src/api/types';
import { api } from './api';
import { expectReady, hybrid, pipelinePath, setupCorpus, waitForPipeline } from './fixtures';

const engineURL = process.env.OCR_LIVE_WORKFLOW_URL;

test('upload a real scan, change the OCR strategy in its schema form, and query both results', async ({ page }, info) => {
  test.skip(!engineURL, 'set OCR_LIVE_WORKFLOW_URL to a real Tesseract shim');
  const prefix = `real-ocr-${info.retry}`;
  const { corpus, embedder, store } = await setupCorpus(prefix, 0);
  const parser = await api<Provider>('POST', '/providers', {
    name: `${prefix}-tesseract`, kind: 'parser', type: 'http',
    config: { base_url: engineURL, timeout_seconds: 120 },
  });

  await page.goto(`/corpora/${corpus.id}/documents`);
  await page.getByTestId('upload-input').setInputFiles(
    fileURLToPath(new URL('../../../tests/ocr_live/fixtures/scan_clean.png', import.meta.url)),
  );
  await page.getByRole('button', { name: /Upload 1 file/ }).click();
  await expect(page.getByRole('button', { name: 'Done', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Done', exact: true }).click();
  await expect(page.getByRole('link', { name: 'scan_clean.png', exact: true })).toBeVisible();

  const config = hybrid(`${prefix}-original`, embedder.name, store.name);
  const parse = config.nodes.find(n => n.type === 'parse')!;
  if (parse.type !== 'parse') throw new Error('missing parse node');
  parse.routes = [{ mime: 'image/*', provider: parser.name, options: { lang: 'eng' } }];
  const original = await api<Pipeline>('POST', `/corpora/${corpus.id}/pipelines`, config);
  await waitForPipeline(original.id);
  await page.goto(pipelinePath(corpus.id, original.id));
  await expectReady(page, original.name);
  const first = await api<QueryResponse>('POST', `/corpora/${corpus.id}/query`, {
    pipeline_id: original.id, query: 'warehouse inspection',
  });
  expect(first.results.some(r => /warehouse/i.test(r.text))).toBeTruthy();

  await page.getByRole('link', { name: 'Clone', exact: true }).click();
  await page.getByRole('group', { name: 'Pipeline steps' }).getByRole('button', { name: /^parse Parse / }).click();
  await expect(page.getByRole('group', { name: 'Parser options' })).toBeVisible();
  await page.getByLabel('lang', { exact: true }).fill('eng+osd');
  await page.getByLabel('lang', { exact: true }).press('Tab');
  const plan = page.getByRole('complementary', { name: 'Plan' });
  await expect(plan.getByRole('button', { name: /^parse Parse new/ })).toBeVisible();
  const response = page.waitForResponse(r => r.url().endsWith(`/v1/corpora/${corpus.id}/pipelines`)
    && r.request().method() === 'POST');
  await page.getByRole('button', { name: 'Create pipeline', exact: true }).click();
  const created = await response;
  expect(created.ok(), await created.text()).toBeTruthy();
  const changed = await created.json() as Pipeline;
  expect(changed.config.nodes.find(n => n.type === 'parse')).toMatchObject({
    routes: [{ mime: 'image/*', provider: parser.name, options: { lang: 'eng+osd' } }],
  });
  expect(changed.nodes.find(n => n.node_id === 'parse')?.node_fp)
    .not.toBe(original.nodes.find(n => n.node_id === 'parse')?.node_fp);
  await expectReady(page, changed.name);
  await page.getByRole('button', { name: 'Set active', exact: true }).click();
  await page.getByRole('link', { name: 'Query', exact: true }).last().click();
  await page.getByRole('textbox', { name: 'Query', exact: true }).fill('warehouse inspection');
  const queried = page.waitForResponse(r => r.url().endsWith(`/v1/corpora/${corpus.id}/query`)
    && r.request().method() === 'POST');
  await page.getByRole('button', { name: 'Search', exact: true }).click();
  const query = await queried;
  expect(query.ok(), await query.text()).toBeTruthy();
  expect(((await query.json()) as QueryResponse).results.some(r => /warehouse/i.test(r.text))).toBeTruthy();
  await expect(page.locator('.qcols').getByText(/warehouse/i).first()).toBeVisible();
});
