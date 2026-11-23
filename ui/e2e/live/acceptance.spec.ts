// IMPLEMENTATION_PLAN.md §21.6. No page.route(), MSW, API interception or database edits.
import { expect, test, type Page } from '@playwright/test';
import type { Corpus, EvalRunDetail, Pipeline, PipelinePlan, Provider, QueryResponse } from '../../src/api/types';
import { api } from './api';
import { documents, expectReady, externalPipeline, parserControl, pipelinePath, readyCorpus, runs, waitForPipeline } from './fixtures';

async function submit<T>(page: Page, path: string, action: () => Promise<unknown>): Promise<T> {
  const response = page.waitForResponse(r => r.url().endsWith(`/v1${path}`) && r.request().method() === 'POST');
  await action();
  const result = await response;
  expect(result.ok(), await result.text()).toBeTruthy();
  return result.json() as Promise<T>;
}

test.beforeEach(async ({ page }) => {
  page.on('pageerror', error => { throw error; });
});

test('1. first run: create providers, corpus, documents, hybrid pipeline, activate and query', async ({ page }, info) => {
  const prefix = `first-${info.retry}`;
  await page.goto('/');
  await expect(page.getByText('Connected', { exact: true })).toBeVisible();
  const created: Provider[] = [];
  for (const [kind, type, suffix] of [['embedder', 'builtin_hash', 'hash'], ['vector_store', 'sqlite_vec', 'sqlite']]) {
    await page.goto('/providers');
    await page.getByRole('link', { name: 'New provider' }).first().click();
    await page.locator('label.choice').filter({ has: page.locator(`input[name="kind"][value="${kind}"]`) }).click();
    await page.locator('label.choice').filter({ has: page.locator(`input[name="type"][value="${type}"]`) }).click();
    await page.getByRole('textbox', { name: 'Name *', exact: true }).fill(`${prefix}-${suffix}`);
    const provider = await submit<Provider>(page, '/providers', () => page.getByRole('button', { name: 'Create provider', exact: true }).click());
    created.push(provider);
    await expect(page.getByRole('heading', { name: provider.name, exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Test connection' }).click();
    await expect(page.getByText('Connection works', { exact: true })).toBeVisible();
  }
  await page.goto('/');
  await page.getByRole('link', { name: 'New corpus' }).first().click();
  await page.getByRole('textbox', { name: 'Name *', exact: true }).fill(`${prefix}-bikes`);
  const corpus = await submit<Corpus>(page, '/corpora', () => page.getByRole('button', { name: 'Create corpus' }).click());
  await page.goto(`/corpora/${corpus.id}/documents`);
  await page.getByTestId('upload-input').setInputFiles(documents);
  await page.getByRole('button', { name: /Upload 2 files/ }).click();
  await expect(page.getByRole('button', { name: 'Done', exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Done', exact: true }).click();
  for (const doc of documents) await expect(page.getByRole('link', { name: doc.name, exact: true })).toBeVisible();
  expect((await api<Corpus>('GET', `/corpora/${corpus.id}`)).document_count).toBe(2);

  await page.goto(`/corpora/${corpus.id}/pipelines`);
  await page.getByRole('link', { name: 'New pipeline' }).first().click();
  await page.getByRole('button', { name: /^Hybrid Vector/ }).click();
  await page.getByRole('textbox', { name: 'Name *', exact: true }).fill(`${prefix}-hybrid`);
  const pipeline = await submit<Pipeline>(page, `/corpora/${corpus.id}/pipelines`, () => page.getByRole('button', { name: 'Create pipeline' }).click());
  expect(pipeline.config.retrieval.sources.map(s => s.id)).toEqual(['dense', 'sparse']);
  expect(pipeline.config.retrieval.fusion?.method).toBe('rrf');
  expect(pipeline.config.nodes.find(n => n.type === 'embed')).toMatchObject({ provider: created[0].name });
  await expectReady(page, pipeline.name);
  await page.getByRole('button', { name: 'Set active', exact: true }).click();
  await expect(page.locator('.prov-detail__title').getByText('active', { exact: true })).toBeVisible();
  expect((await api<Corpus>('GET', `/corpora/${corpus.id}`)).active_pipeline_id).toBe(pipeline.id);
  await page.getByRole('link', { name: 'Query', exact: true }).last().click();
  await page.getByRole('textbox', { name: 'Query', exact: true }).fill('lifetime warranty');
  const result = await submit<QueryResponse>(page, `/corpora/${corpus.id}/query`, () => page.getByRole('button', { name: 'Search', exact: true }).click());
  expect(result.results.length).toBeGreaterThan(0);
  expect(result.results.some(r => r.text.includes('lifetime warranty'))).toBeTruthy();
  await expect(page.locator('.qcols').getByText(/Frames carry a lifetime warranty/).first()).toBeVisible();
});

test('2. swap the chunker: clone, plan parse reuse, build and inspect diff', async ({ page }, info) => {
  const { corpus, pipelines: [original] } = await readyCorpus(`clone-${info.retry}`);
  const before = (await runs(original.id)).filter(r => r.node_type === 'parse');
  expect(before).toHaveLength(2);
  await page.goto(pipelinePath(corpus.id, original.id));
  await page.getByRole('link', { name: 'Clone', exact: true }).click();
  await page.getByLabel('Strategy', { exact: true }).selectOption('fixed');
  await page.getByLabel('Max characters', { exact: true }).fill('100');
  await page.getByLabel('Max characters', { exact: true }).press('Tab');
  await page.getByLabel('Overlap characters', { exact: true }).fill('0');
  await page.getByLabel('Overlap characters', { exact: true }).press('Tab');
  const plan = page.getByRole('complementary', { name: 'Plan' });
  await expect(plan.getByRole('button', { name: /^parse Parse reused/ })).toBeVisible();
  await expect(plan.getByRole('button', { name: /^chunk Chunk new/ })).toBeVisible();
  await expect(plan.getByText('Valid', { exact: true })).toBeVisible();
  const clone = await submit<Pipeline>(page, `/corpora/${corpus.id}/pipelines`, () => page.getByRole('button', { name: 'Create pipeline' }).click());
  await expectReady(page, clone.name);
  const after = (await runs(clone.id)).filter(r => r.node_type === 'parse');
  expect(after.map(r => r.id).sort()).toEqual(before.map(r => r.id).sort());
  expect(after.map(r => r.attempt)).toEqual(before.map(r => r.attempt));
  expect(clone.nodes.find(n => n.node_id === 'parse')?.node_fp).toBe(original.nodes.find(n => n.node_id === 'parse')?.node_fp);
  expect(clone.nodes.find(n => n.node_id === 'chunk')?.node_fp).not.toBe(original.nodes.find(n => n.node_id === 'chunk')?.node_fp);
  await page.getByLabel('Compare with', { exact: true }).selectOption(original.id);
  await expect(page.getByRole('heading', { name: 'Compare pipelines' })).toBeVisible();
  await expect(page.getByRole('row').filter({ has: page.getByRole('cell', { name: 'parse Parse', exact: true }) })).toContainText('identical');
  await expect(page.getByRole('row').filter({ has: page.getByRole('cell', { name: 'strategy', exact: true }) })).toContainText('structure');
  await expect(page.getByRole('row').filter({ has: page.getByRole('cell', { name: 'strategy', exact: true }) })).toContainText('fixed');
  const dryRun = await api<PipelinePlan>('POST', `/corpora/${corpus.id}/pipelines?dry_run=true`, { ...clone.config, name: `${clone.config.name}-dry` });
  expect(dryRun.valid).toBe(true);
  if (dryRun.valid) expect(dryRun.total_runs_to_create).toBe(0);
});

test('3. compare by evaluation: upload dataset, run two pipelines, read comparison and item', async ({ page }, info) => {
  const { corpus, pipelines } = await readyCorpus(`eval-${info.retry}`, true);
  await page.goto(`/corpora/${corpus.id}/eval`);
  await page.getByRole('button', { name: 'Upload dataset', exact: true }).first().click();
  await page.getByRole('textbox', { name: 'Name', exact: true }).fill('bike-questions');
  const questions = [
    { question: 'lifetime warranty', targets: [{ document: 'warranty.md', text_contains: 'lifetime warranty' }] },
    { question: 'Express delivery', targets: [{ document: 'shipping.md', text_contains: 'next working day' }] },
  ];
  await page.getByTestId('dataset-input').setInputFiles({ name: 'questions.jsonl', mimeType: 'application/x-ndjson', buffer: Buffer.from(questions.map(q => JSON.stringify(q)).join('\n')) });
  await page.getByRole('button', { name: 'Import dataset' }).click();
  await expect(page.getByRole('link', { name: 'bike-questions', exact: true })).toBeVisible();
  await page.goto(`/corpora/${corpus.id}/eval`);
  await page.getByRole('button', { name: 'New run', exact: true }).click();
  for (const pipeline of pipelines) await page.getByRole('checkbox', { name: pipeline.name, exact: true }).check();
  await page.getByLabel('k values', { exact: true }).fill('1, 3, 10');
  await page.getByRole('button', { name: 'Start run (2 pipelines)', exact: true }).click();
  await expect(page).toHaveURL(/\/eval\/runs\/[^/]+$/);
  const runId = new URL(page.url()).pathname.split('/').at(-1)!;
  await expect(page.locator('.prov-detail__title').getByText('Finished', { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole('columnheader', { name: '95% CI', exact: true })).toBeVisible();
  await expect(page.getByRole('columnheader', { name: 'Significant', exact: true })).toBeVisible();
  const run = await api<EvalRunDetail>('GET', `/eval/runs/${runId}`);
  expect(run.status).toBe('finished');
  expect(run.item_count).toBe(2);
  expect(run.progress).toEqual({ done: 4, total: 4 });
  expect(run.summaries).toHaveLength(2);
  for (const summary of run.summaries) {
    expect(summary.error_count).toBe(0);
    expect(summary.metrics['recall@10']).toBeGreaterThanOrEqual(0.5);
  }
  await page.getByRole('link', { name: 'Questions where recall@10 differs', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Questions that changed' })).toBeVisible();
  await page.getByRole('tab', { name: /Unchanged/ }).click();
  const item = page.getByRole('button', { name: /^lifetime warranty/ });
  await item.click();
  await expect(item).toHaveAttribute('aria-expanded', 'true');
  for (const pipeline of pipelines) {
    const results = page.getByRole('region', { name: `${pipeline.name} results` });
    await expect(results).toBeVisible();
    await expect(results.getByLabel('matches target 1', { exact: true }).first()).toBeVisible();
    await expect(results.getByText(/lifetime warranty/).first()).toBeVisible();
  }
});

test('4. recover from failure: inspect real parser error, repair service, retry and unblock descendants', async ({ page }, info) => {
  const key = `recover-${info.retry}`;
  const { corpus, pipeline } = await externalPipeline(key, 'fail');
  try {
    const degraded = await waitForPipeline(pipeline.id, 'degraded');
    expect(degraded.nodes.find(n => n.node_id === 'parse')?.counts.failed).toBe(1);
    expect(degraded.nodes.filter(n => n.node_id !== 'parse').every(n => n.counts.blocked === 1)).toBeTruthy();
    const before = await runs(pipeline.id);
    await page.goto(pipelinePath(corpus.id, pipeline.id));
    const failure = page.getByRole('row').filter({ hasText: 'upstream returned 422' });
    await expect(failure).toContainText('provider_error');
    await expect(failure).not.toContainText('E2E parser maintenance: repair service and retry this run');
    await expect(page.getByText('1 blocked', { exact: false }).first()).toBeVisible();
    await parserControl(key, 'ok');
    await failure.getByRole('button', { name: 'Retry', exact: true }).click();
    await expectReady(page, pipeline.name);
    await expect(page.getByText('No failed runs', { exact: true })).toBeVisible();
    const after = await runs(pipeline.id);
    expect(after.map(r => r.id).sort()).toEqual(before.map(r => r.id).sort());
    expect(after).toHaveLength(5);
    expect(after.every(r => r.status === 'succeeded')).toBeTruthy();
    const service = await parserControl(key);
    expect(service.requests).toBe(2);
    expect(service.completed).toBe(1);
  } finally {
    await parserControl(key, 'ok');
  }
});

test('5. guard rails: building query error, permanent provider settings and active archive refusal', async ({ page, request }, info) => {
  const key = `guards-${info.retry}`;
  const { corpus, pipeline, embedder } = await externalPipeline(key, 'hold');
  try {
    await expect.poll(async () => (await parserControl(key)).waiting).toBe(1);
    expect((await api<Pipeline>('GET', `/pipelines/${pipeline.id}`)).status).toBe('building');
    await page.goto(`/corpora/${corpus.id}/query?a=${pipeline.id}`);
    await page.getByRole('textbox', { name: 'Query', exact: true }).fill('warranty');
    const denied = page.waitForResponse(r => r.url().endsWith(`/corpora/${corpus.id}/query`) && r.request().method() === 'POST');
    await page.getByRole('button', { name: 'Search', exact: true }).click();
    const response = await denied;
    expect(response.status()).toBe(409);
    expect((await response.json()).error.code).toBe('index_not_built');
    await expect(page.getByText(/index_not_built/)).toBeVisible();
    await expect(page.getByText(/not been built yet/i).first()).toBeVisible();

    await page.goto(`/providers/${embedder.id}`);
    const permanent = page.getByRole('region', { name: 'Permanent settings' });
    await expect(permanent).toBeVisible();
    await expect(permanent).toContainText(/fingerprint/);
    await expect(permanent).toContainText(/cannot|can't|permanent|locked/i);
    await expect(permanent.getByRole('textbox')).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'New provider from this one', exact: true })).toBeVisible();
    const deniedEdit = await request.patch(`/v1/providers/${embedder.id}`, { data: { config: { dimensions: 128 } } });
    expect(deniedEdit.status()).toBe(409);
    expect((await deniedEdit.json()).error.code).toBe('immutable_field');
    expect((await api<Provider>('GET', `/providers/${embedder.id}`)).fingerprint).toBe(embedder.fingerprint);

    await parserControl(key, 'ok');
    await waitForPipeline(pipeline.id);
    await page.goto(pipelinePath(corpus.id, pipeline.id));
    await expectReady(page, pipeline.name);
    await page.getByRole('button', { name: 'Set active', exact: true }).click();
    await expect(page.locator('.prov-detail__title').getByText('active', { exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Archive', exact: true }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog).toContainText('server will refuse');
    const archived = page.waitForResponse(r => r.url().endsWith(`/pipelines/${pipeline.id}/archive`));
    await dialog.getByRole('button', { name: 'Archive pipeline', exact: true }).click();
    expect((await archived).status()).toBe(409);
    await expect(dialog.getByRole('alert')).toContainText(/active/i);
    expect((await api<Pipeline>('GET', `/pipelines/${pipeline.id}`)).archived).toBe(false);
    expect((await api<Corpus>('GET', `/corpora/${corpus.id}`)).active_pipeline_id).toBe(pipeline.id);
  } finally {
    await parserControl(key, 'ok');
  }
});
