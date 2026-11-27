// Exercise real browser setup once; every test receives an independent authenticated context.
import { expect, chromium } from '@playwright/test';
import { randomBytes } from 'node:crypto';
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { api } from './api';

export default async function globalSetup(): Promise<void> {
  const config = await api<{ setup_required: boolean; setup_allowed: boolean }>('GET', '/auth/config');
  if (!config.setup_required || !config.setup_allowed) throw new Error('Live acceptance requires a fresh loopback installation');
  const corpora = await api<{ items: unknown[] }>('GET', '/corpora');
  if (corpora.items.length) throw new Error('Live acceptance requires an empty, isolated data directory');

  const baseURL = `http://127.0.0.1:${process.env.RAGCENTRE_E2E_PORT ?? 8711}`;
  const dataDir = process.env.RAGCENTRE_E2E_DATA_DIR!;
  const username = 'release-browser';
  const password = randomBytes(24).toString('hex');
  const browser = await chromium.launch();
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    await page.goto(baseURL);
    await expect(page.getByRole('heading', { name: 'Create your account' })).toBeVisible();
    await page.getByRole('textbox', { name: 'Username', exact: true }).fill(username);
    await page.getByRole('textbox', { name: 'Display name', exact: true }).fill('Release Browser');
    await page.getByLabel('Password', { exact: true }).fill(password);
    await page.getByLabel('Confirm password', { exact: true }).fill(password);
    await page.getByRole('button', { name: 'Create account', exact: true }).click();
    await expect(page.getByText('Connected', { exact: true })).toBeVisible();

    const csrfResponse = await context.request.get(`${baseURL}/v1/auth/csrf`);
    expect(csrfResponse.status()).toBe(200);
    const { token: csrf } = await csrfResponse.json() as { token: string };
    const keyResponse = await context.request.post(`${baseURL}/v1/keys`, {
      headers: { 'X-RAGCentre-CSRF': csrf }, data: { name: 'e2e-fixture-helper', scope: 'full' },
    });
    expect(keyResponse.status()).toBe(201);
    const { key } = await keyResponse.json() as { key: string };
    // Ephemeral secrets stay in the temporary backend fixture directory, outside the repository.
    writeFileSync(join(dataDir, 'e2e-credentials.json'), JSON.stringify({ username, password, key }), { mode: 0o600 });
    const state = await context.storageState();
    writeFileSync(join(dataDir, 'e2e-browser-state.json'), JSON.stringify(state), { mode: 0o600 });
    await context.close();
  } finally {
    await browser.close();
  }
}
