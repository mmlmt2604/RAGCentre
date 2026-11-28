import { expect, test } from '@playwright/test';
import { parserControl } from './fixtures';

test('a running OCR shim is found, added in one click, proven, and reported when it breaks', async ({ page }) => {
  await parserControl('ocr-shim', 'ok');
  await page.goto('/providers');
  const found = page.getByRole('region', { name: 'OCR shims found' });
  await expect(found).toContainText('e2e-ocr-shim');
  expect((await parserControl('ocr-shim')).requests).toBe(0);

  await found.getByRole('button', { name: 'Add and test OCR' }).click();
  const link = page.getByRole('link', { name: 'e2e-ocr', exact: true });
  await expect(link).toBeFocused();
  await expect(page.locator('.shell-status__ocr')).toHaveText(/^OCR ready(?: \(\d+\))?$/);
  await expect(found).toBeHidden();

  await link.click();
  const panel = page.locator('.ocr-panel');
  await expect(panel).toContainText('OCR engine running');
  await expect(panel).toContainText('Scanned text read');

  await parserControl('ocr-shim', 'fail');
  await panel.getByRole('button', { name: 'Test OCR' }).click();
  await expect(panel).toContainText('OCR engine not working');
  await expect(page.locator('.shell-status__ocr')).toHaveText(/^(OCR not working|\d+ of \d+ OCR ready)$/);

  await parserControl('ocr-shim', 'ok');
  await panel.getByRole('button', { name: 'Test OCR' }).click();
  await expect(panel).toContainText('OCR engine running');
});
