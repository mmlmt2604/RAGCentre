import { expect, test } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

function credentials(): { username: string; password: string } {
  const { username, password } = JSON.parse(readFileSync(join(process.env.RAGCENTRE_E2E_DATA_DIR!, 'e2e-credentials.json'), 'utf8')) as { username: string; password: string };
  return { username, password };
}

test('an unauthenticated deep link signs in and returns to the requested page', async ({ browser }) => {
  const context = await browser.newContext({ storageState: { cookies: [], origins: [] } });
  try {
    const page = await context.newPage();
    await page.goto(`http://127.0.0.1:${process.env.RAGCENTRE_E2E_PORT ?? 8711}/providers`);
    await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible();
    const { username, password } = credentials();
    await page.getByRole('textbox', { name: 'Username', exact: true }).fill(username);
    await page.getByLabel('Password', { exact: true }).fill(password);
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page).toHaveURL(/\/providers$/);
    await expect(page.getByRole('heading', { name: 'Providers', exact: true })).toBeVisible();
    expect((await context.request.get(`http://127.0.0.1:${process.env.RAGCENTRE_E2E_PORT ?? 8711}/v1/auth/me`)).status()).toBe(200);
  } finally {
    await context.close();
  }
});

test('signing out one browser session does not revoke another', async ({ browser, request, baseURL }) => {
  const context = await browser.newContext({ storageState: { cookies: [], origins: [] } });
  try {
    const login = await context.request.post(`${baseURL}/v1/auth/login`, { data: credentials() });
    expect(login.status()).toBe(200);
    const page = await context.newPage();
    await page.goto(`${baseURL}/account`);
    await expect(page.getByRole('heading', { name: 'Account', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Sign out', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible();
    expect((await context.request.get(`${baseURL}/v1/auth/me`)).status()).toBe(401);
    expect((await request.get('/v1/auth/me')).status()).toBe(200);
  } finally {
    await context.close();
  }
});

test('a read key is revealed once, limited to reads, and revoked from Account', async ({ page, request }) => {
  // Do not retain a trace containing the newly revealed bearer secret.
  await page.goto('/account');
  await page.getByRole('button', { name: 'New API key', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'New API key', exact: true });
  await dialog.getByRole('textbox', { name: 'Name', exact: true }).fill('browser-one-time-read-key');
  const created = page.waitForResponse(r => r.url().endsWith('/v1/keys') && r.request().method() === 'POST');
  await dialog.getByRole('button', { name: 'Create key', exact: true }).click();
  const response = await created;
  expect(response.status()).toBe(201);
  const { key, id, scope } = await response.json() as { key: string; id: string; scope: string };
  expect(scope).toBe('read');
  const reveal = page.getByRole('dialog', { name: 'API key created', exact: true });
  await expect(reveal).toContainText('shown exactly once');
  expect(await reveal.locator('code').textContent() === key).toBe(true);
  await reveal.getByRole('button', { name: 'Done', exact: true }).click();
  await page.reload();
  expect((await page.locator('body').textContent())?.includes(key)).toBe(false);
  const listed = await request.get('/v1/keys');
  expect((await listed.text()).includes(key)).toBe(false);
  const headers = { Authorization: `Bearer ${key}` };
  expect((await request.get('/v1/corpora', { headers })).status()).toBe(200);
  expect((await request.post('/v1/corpora', { headers, data: { name: 'must-not-be-created' } })).status()).toBe(403);
  const row = page.getByRole('row').filter({ hasText: 'browser-one-time-read-key' });
  await row.getByRole('button', { name: 'Revoke', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Revoke key', exact: true }).click();
  await expect(row).toHaveCount(0);
  expect((await request.get('/v1/corpora', { headers })).status()).toBe(401);
  const after = await request.get('/v1/keys');
  expect((await after.text()).includes(id)).toBe(false);
});

test('setup stays closed and existing sessions reload Account', async ({ page, request }) => {
  const config = await request.get('/v1/auth/config');
  expect((await config.json()).setup_required).toBe(false);
  expect((await request.post('/v1/auth/setup', { data: { username: 'second-user', password: 'another-release-password' } })).status()).toBe(403);
  await page.goto('/account');
  await expect(page.getByRole('heading', { name: 'Account', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('heading', { name: 'Account', exact: true })).toBeVisible();
});

test.use({ trace: 'off' });
