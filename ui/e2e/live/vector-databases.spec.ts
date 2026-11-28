import { expect, test, type Page } from '@playwright/test';
import { randomUUID } from 'node:crypto';
import type { Corpus, Pipeline, Provider, ProviderType, QueryResponse } from '../../src/api/types';
import { api } from './api';
import { documents, hybrid, waitForPipeline } from './fixtures';

test.skip(process.env.RAGCENTRE_E2E_VECTOR_STORES !== '1', 'Set RAGCENTRE_E2E_VECTOR_STORES=1 with the local database fixtures running');

type Profile = {
  type: string;
  config: Record<string, string | number | boolean>;
  secrets?: Record<string, string>;
  tune?: [string, number];
};

const profiles: Profile[] = [
  { type: 'sqlite_vec', config: {} },
  { type: 'http', config: { url: `http://127.0.0.1:${Number(process.env.RAGCENTRE_E2E_PORT ?? 8711) + 2000}` } },
  { type: 'qdrant', config: { url: 'http://127.0.0.1:56333', quantization: 'binary' }, tune: ['hnsw_ef', 96] },
  { type: 'pgvector', config: { host: '127.0.0.1', port: 54329, dbname: 'ragcentre', user: 'ragcentre', vector_type: 'halfvec' }, secrets: { password: 'RAGCENTRE_PROVIDER_E2E_PG_PASSWORD' }, tune: ['ef_search', 96] },
  { type: 'chroma', config: { mode: 'http', host: '127.0.0.1', port: 58000 }, tune: ['ef_search', 96] },
  { type: 'lancedb', config: {} },
  { type: 'redis', config: { url: 'redis://127.0.0.1:56379/0', algorithm: 'hnsw' }, tune: ['ef_runtime', 96] },
  { type: 'weaviate', config: { url: 'http://127.0.0.1:58080', grpc_port: 50051 }, tune: ['hnsw_ef', 96] },
  { type: 'elasticsearch', config: { url: 'http://127.0.0.1:59200', index_options: 'int8_hnsw' }, tune: ['num_candidates_factor', 20] },
  { type: 'opensearch', config: { url: 'http://127.0.0.1:59201', engine: 'lucene' } },
  { type: 'milvus', config: { uri: 'http://127.0.0.1:59530', index_type: 'HNSW' }, tune: ['hnsw_ef', 96] },
  { type: 'pinecone', config: { controller_host: 'http://localhost:55080' }, secrets: { api_key: 'RAGCENTRE_PROVIDER_E2E_PINECONE_KEY' } },
  { type: 'mongodb', config: { exact: false }, secrets: { uri: 'RAGCENTRE_PROVIDER_E2E_MONGODB_URI' }, tune: ['num_candidates_factor', 17] },
];

async function setField(page: Page, key: string, value: string | number | boolean) {
  const field = page.locator('.rc-field').filter({ has: page.locator('.cfg-key').filter({ hasText: new RegExp(`^${key}$`) }) });
  const input = field.locator('input, select, textarea').first();
  if (typeof value === 'boolean') {
    if (await input.isChecked() !== value) await field.locator('label.rc-check').click();
  } else if (await input.evaluate(element => element.tagName) === 'SELECT') await input.selectOption(String(value));
  else await input.fill(String(value));
}

for (const profile of profiles) {
  test(`vector database ${profile.type}: configure, ingest, query and tune`, async ({ page }) => {
    test.setTimeout(120_000);
    page.setDefaultTimeout(15_000);
    const prefix = `browser-${profile.type.replaceAll('_', '-')}-${randomUUID().slice(0, 8)}`;
    const namespace = `br${randomUUID().replaceAll('-', '').slice(0, 24)}`;
    let pineconeIndex: string | undefined;
    let pipeline: Pipeline | undefined;
    const pineconeHeaders = { 'Api-Key': 'local-key', 'X-Pinecone-Api-Version': '2025-01', 'Content-Type': 'application/json' };
    const config = { ...profile.config };
    if (profile.type === 'pgvector') config.schema = namespace;
    else if (profile.type !== 'http') config.prefix = `${namespace}_`;
    if (profile.type === 'pinecone') {
      pineconeIndex = `rct-${randomUUID().slice(0, 12)}`;
      const response = await fetch('http://localhost:55080/indexes', {
        method: 'POST', headers: pineconeHeaders,
        body: JSON.stringify({ name: pineconeIndex, dimension: 256, metric: 'cosine', spec: { serverless: { cloud: 'aws', region: 'us-east-1' } } }),
      });
      expect(response.ok, await response.text()).toBeTruthy();
      config.index_name = pineconeIndex;
    }
    try {
      const types = await api<{ items: ProviderType[] }>('GET', '/provider-types?limit=100');
      const type = types.items.find(item => item.kind === 'vector_store' && item.type === profile.type)!;
      expect(type).toBeDefined();
      await page.goto('/providers/new');
      await page.locator('label.choice').filter({ has: page.locator('input[name="kind"][value="vector_store"]') }).click();
      await page.locator('label.choice').filter({ has: page.locator(`input[name="type"][value="${profile.type}"]`) }).click();
      await page.getByRole('textbox', { name: 'Name *', exact: true }).fill(prefix);
      for (const [key, value] of Object.entries(config)) await setField(page, key, value);
      for (const key of type.secret_keys) {
        const secret = page.locator('.secret-in').filter({ has: page.locator('.secret-in__key').filter({ hasText: new RegExp(`^${key}$`) }) });
        if (profile.secrets?.[key]) await secret.getByRole('textbox').fill(profile.secrets[key]);
        else await secret.locator('label').filter({ has: page.locator('input[value="skip"]') }).click();
      }
      const created = page.waitForResponse(response => response.url().endsWith('/v1/providers') && response.request().method() === 'POST');
      await page.getByRole('button', { name: /^(Create provider|Connect and create)$/ }).click();
      const response = await created;
      expect(response.status(), await response.text()).toBe(201);
      const store = await response.json() as Provider;
      await expect(page.getByRole('heading', { name: prefix, exact: true })).toBeVisible();
      await page.getByRole('button', { name: 'Test connection', exact: true }).click();
      await expect(page.getByText('Connection works', { exact: true })).toBeVisible({ timeout: 30_000 });

      const embedder = await api<Provider>('POST', '/providers', { name: `${prefix}-hash`, kind: 'embedder', type: 'builtin_hash', config: {} });
      const corpus = await api<Corpus>('POST', '/corpora', { name: prefix });
      for (const doc of documents) {
        const form = new FormData();
        form.set('file', new Blob([doc.buffer], { type: doc.mimeType }), doc.name);
        form.set('external_id', doc.name);
        await api('POST', `/corpora/${corpus.id}/documents`, form);
      }
      const pipelineConfig = hybrid(prefix, embedder.name, store.name);
      pipelineConfig.nodes = pipelineConfig.nodes.filter(node => node.type !== 'index_fts');
      pipelineConfig.retrieval = { sources: [{ id: 'dense', index: 'vec', top_k: 5 }], top_k: 5 };
      pipeline = await api<Pipeline>('POST', `/corpora/${corpus.id}/pipelines`, pipelineConfig);
      await waitForPipeline(pipeline.id);

      if (profile.tune) {
        const [key, value] = profile.tune;
        expect(store.config[key], `${key} must change from its initial value`).not.toBe(value);
        const before = store.fingerprint;
        await page.goto(`/providers/${store.id}`);
        await setField(page, key, value);
        await page.getByRole('button', { name: 'Save settings', exact: true }).click();
        await expect.poll(async () => (await api<Provider>('GET', `/providers/${store.id}`)).config[key]).toBe(value);
        await page.reload();
        expect((await api<Provider>('GET', `/providers/${store.id}`)).fingerprint).toBe(before);
        const field = page.locator('.rc-field').filter({ has: page.locator('.cfg-key').filter({ hasText: new RegExp(`^${key}$`) }) });
        await expect(field.locator('input')).toHaveValue(String(value));
      }
      await page.goto(`/corpora/${corpus.id}/query?a=${pipeline.id}`);
      await page.getByRole('textbox', { name: 'Query', exact: true }).fill('lifetime warranty');
      const queried = page.waitForResponse(result => result.url().endsWith(`/v1/corpora/${corpus.id}/query`) && result.request().method() === 'POST');
      await page.getByRole('button', { name: 'Search', exact: true }).click();
      const queryResponse = await queried;
      expect(queryResponse.status(), await queryResponse.text()).toBe(200);
      const result = await queryResponse.json() as QueryResponse;
      expect(result.results.some(item => item.text.includes('lifetime warranty'))).toBeTruthy();
      await expect(page.locator('.qcols').getByText(/Frames carry a lifetime warranty/).first()).toBeVisible();
    } finally {
      if (pipeline) {
        await api('POST', `/pipelines/${pipeline.id}/archive`);
        await api('POST', '/admin/gc', { dry_run: false });
      }
      if (pineconeIndex) {
        const removed = await fetch(`http://localhost:55080/indexes/${pineconeIndex}`, { method: 'DELETE', headers: pineconeHeaders });
        expect(removed.ok).toBeTruthy();
      }
    }
  });
}
