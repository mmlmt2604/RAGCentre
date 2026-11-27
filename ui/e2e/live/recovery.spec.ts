import { expect, test } from '@playwright/test';
import { spawn, spawnSync } from 'node:child_process';
import { once } from 'node:events';
import { mkdtempSync, rmSync } from 'node:fs';
import { createServer } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

// Credential replacement revokes every session. Use a separate backend/account so this
// journey can run in any order without invalidating the ordinary suite's setup session.
test('password change and local CLI recovery restore usable browser access', async ({ browser }) => {
  const dataDir = mkdtempSync(join(tmpdir(), 'ragcentre-browser-recovery-'));
  const socket = createServer();
  socket.listen(0, '127.0.0.1');
  await once(socket, 'listening');
  const address = socket.address();
  if (!address || typeof address === 'string') throw new Error('Expected TCP listener');
  const port = address.port;
  await new Promise<void>((resolve, reject) => socket.close(error => error ? reject(error) : resolve()));
  const base = `http://127.0.0.1:${port}`;
  const cwd = fileURLToPath(new URL('../../../', import.meta.url));
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !key.startsWith('RAGCENTRE_')));
  Object.assign(env, { RAGCENTRE_DATA_DIR: dataDir, RAGCENTRE_HOST: '127.0.0.1', RAGCENTRE_DISCOVERY_URLS: 'off' });
  const server = spawn('uv', ['run', 'ragcentre', 'serve', '--port', String(port), '--workers', '0'], { cwd, env, stdio: 'ignore', detached: true });
  const context = await browser.newContext({ storageState: { cookies: [], origins: [] } });
  try {
    await expect.poll(async () => {
      if (server.exitCode !== null) throw new Error('Recovery fixture server exited');
      return context.request.get(`${base}/v1/health`).then(r => r.status()).catch(() => 0);
    }).toBe(200);
    const page = await context.newPage();
    const original = 'browser-recovery-original-password';
    const replacement = 'browser-recovery-replacement-password';
    const recovered = 'browser-recovery-local-reset-password';
    await page.goto(base);
    await page.getByRole('textbox', { name: 'Username', exact: true }).fill('recovery-browser');
    await page.getByLabel('Password', { exact: true }).fill(original);
    await page.getByLabel('Confirm password', { exact: true }).fill(original);
    await page.getByRole('button', { name: 'Create account', exact: true }).click();
    await expect(page.getByText('Connected', { exact: true })).toBeVisible();
    await page.goto(`${base}/account`);
    await expect(page.getByRole('definition').filter({ hasText: 'recovery-browser' })).toBeVisible();
    await page.getByLabel('Current password', { exact: true }).fill(original);
    await page.getByLabel('New password', { exact: true }).fill(replacement);
    await page.getByLabel('Confirm new password', { exact: true }).fill(replacement);
    await page.getByRole('button', { name: 'Change password', exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible();

    let recoveryCount = 0;
    async function signIn(password: string) {
      await page.getByRole('textbox', { name: 'Username', exact: true }).fill('recovery-browser');
      await page.getByLabel('Password', { exact: true }).fill(password);
      await page.getByRole('button', { name: 'Sign in', exact: true }).click();
      await expect(page.getByText('Connected', { exact: true })).toBeVisible();
      await page.goto(`${base}/account`);
      await expect(page.getByRole('heading', { name: 'Account', exact: true })).toBeVisible();
      expect((await context.request.get(`${base}/v1/auth/me`)).status()).toBe(200);
      const csrf = await (await context.request.get(`${base}/v1/auth/csrf`)).json() as { token: string };
      expect((await context.request.post(`${base}/v1/corpora`, { data: { name: `recovered-browser-${++recoveryCount}` }, headers: { 'X-RAGCentre-CSRF': csrf.token } })).status()).toBe(201);
    }

    await signIn(replacement);
    const reset = spawnSync('uv', ['run', 'ragcentre', 'account', 'reset-password'], { cwd, env, input: `${recovered}\n${recovered}\n`, encoding: 'utf8', timeout: 15_000 });
    expect(reset.status).toBe(0);
    expect((await context.request.get(`${base}/v1/auth/me`)).status()).toBe(401);
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Sign in', exact: true })).toBeVisible();
    await signIn(recovered);
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Account', exact: true })).toBeVisible();
  } finally {
    if (server.pid && server.exitCode === null && server.signalCode === null) {
      const exited = once(server, 'exit');
      process.kill(-server.pid, 'SIGTERM');
      const force = setTimeout(() => {
        try { process.kill(-server.pid!, 'SIGKILL'); } catch { /* Already exited. */ }
      }, 5000);
      await exited;
      clearTimeout(force);
    }
    await context.close();
    rmSync(dataDir, { recursive: true, force: true });
  }
});

test.use({ trace: 'off' });
