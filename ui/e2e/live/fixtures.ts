import { expect, type Page } from '@playwright/test';
import type { Corpus, NodeRun, Pipeline, PipelineConfig, PipelineStatus, Provider } from '../../src/api/types';
import { api } from './api';

export const documents = [
  { name: 'warranty.md', mimeType: 'text/markdown', buffer: Buffer.from('# Warranty\n\nFrames carry a lifetime warranty. Carbon frames are covered for seven years.\n\n## Claims\n\nBring the bike and the receipt to any store.\n') },
  { name: 'shipping.md', mimeType: 'text/markdown', buffer: Buffer.from('# Shipping\n\nStandard delivery takes two to four working days.\n\n## Express\n\nExpress delivery arrives the next working day.\n') },
];

export function hybrid(name: string, embedder: string, store: string, parser = 'builtin-text', strategy: 'structure' | 'fixed' = 'structure'): PipelineConfig {
  return {
    name,
    nodes: [
      { id: 'parse', type: 'parse', routes: [{ mime: 'text/*', provider: parser }] },
      { id: 'chunk', type: 'chunk', inputs: ['parse'], strategy, options: strategy === 'fixed' ? { max_chars: 100, overlap_chars: 0 } : { max_chars: 1200 } },
      { id: 'embed', type: 'embed', inputs: ['chunk'], provider: embedder },
      { id: 'vec', type: 'index_vector', inputs: ['embed'], provider: store, options: { metric: 'cosine' } },
      { id: 'fts', type: 'index_fts', inputs: ['chunk'] },
    ],
    retrieval: { sources: [{ id: 'dense', index: 'vec', top_k: 20 }, { id: 'sparse', index: 'fts', top_k: 20 }], fusion: { method: 'rrf', k: 60 }, top_k: 10 },
  };
}

export async function setupCorpus(prefix: string, documentCount = 2) {
  const embedder = await api<Provider>('POST', '/providers', { name: `${prefix}-hash`, kind: 'embedder', type: 'builtin_hash', config: {} });
  const store = await api<Provider>('POST', '/providers', { name: `${prefix}-sqlite`, kind: 'vector_store', type: 'sqlite_vec', config: {} });
  const corpus = await api<Corpus>('POST', '/corpora', { name: `${prefix}-bikes` });
  for (const doc of documents.slice(0, documentCount)) {
    const form = new FormData();
    form.set('file', new Blob([doc.buffer], { type: doc.mimeType }), doc.name);
    form.set('external_id', doc.name);
    await api('POST', `/corpora/${corpus.id}/documents`, form);
  }
  return { corpus, embedder, store };
}

export async function waitForPipeline(id: string, status: PipelineStatus['status'] = 'ready') {
  await expect.poll(async () => (await api<PipelineStatus>('GET', `/pipelines/${id}/status`)).status, {
    message: `pipeline ${id} reaches ${status}`, timeout: 30_000,
  }).toBe(status);
  return api<PipelineStatus>('GET', `/pipelines/${id}/status`);
}

export async function readyCorpus(prefix: string, two = false) {
  const setup = await setupCorpus(prefix);
  const config = hybrid(`${prefix}-structure`, setup.embedder.name, setup.store.name);
  const original = await api<Pipeline>('POST', `/corpora/${setup.corpus.id}/pipelines`, config);
  await waitForPipeline(original.id);
  await api('PUT', `/corpora/${setup.corpus.id}/active-pipeline`, { pipeline_id: original.id });
  const pipelines = [original];
  if (two) {
    const fixed = await api<Pipeline>('POST', `/corpora/${setup.corpus.id}/pipelines`, hybrid(`${prefix}-fixed`, setup.embedder.name, setup.store.name, 'builtin-text', 'fixed'));
    await waitForPipeline(fixed.id);
    pipelines.push(fixed);
  }
  return { ...setup, pipelines };
}

export async function runs(id: string) {
  return (await api<{ items: NodeRun[] }>('GET', `/node-runs?pipeline_id=${id}&limit=100`)).items;
}

export function pipelinePath(corpusId: string, pipelineId: string) {
  return `/corpora/${corpusId}/pipelines/${pipelineId}`;
}

export async function expectReady(page: Page, name: string) {
  await expect(page.getByRole('heading', { name, exact: true })).toBeVisible();
  await expect(page.locator('.prov-detail__title').getByText('Ready', { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('100%', { exact: true })).toBeVisible();
}

export const fixtureURL = `http://127.0.0.1:${process.env.RAGCENTRE_E2E_FIXTURE_PORT}`;
export async function parserControl(key: string, mode?: 'ok' | 'fail' | 'hold') {
  const response = await fetch(`${fixtureURL}/${key}/control`, mode ? { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ mode }) } : undefined);
  expect(response.ok).toBeTruthy();
  return response.json() as Promise<{ mode: string; requests: number; completed: number; waiting: number; lastOptions: Record<string, unknown> | null }>;
}

export async function externalPipeline(prefix: string, mode: 'fail' | 'hold') {
  await parserControl(prefix, mode);
  const setup = await setupCorpus(prefix, 1);
  await api('POST', '/providers', { name: `${prefix}-parser`, kind: 'parser', type: 'http', config: { base_url: `${fixtureURL}/${prefix}`, timeout_seconds: 120 } });
  const pipeline = await api<Pipeline>('POST', `/corpora/${setup.corpus.id}/pipelines`, hybrid(`${prefix}-external`, setup.embedder.name, setup.store.name, `${prefix}-parser`));
  return { ...setup, pipeline };
}
