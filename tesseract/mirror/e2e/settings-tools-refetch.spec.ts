/**
 * Phase 14 / M1+M4 — Settings Tools section integration coverage.
 *
 * Verifies the two integration paths route-level tests cannot:
 *   1. Switching security mode triggers a refetch of /api/tools so the UI
 *      reflects mode-aware posture (the M1 bug was a stale cache).
 *   2. A failing /api/settings/tool-permission rolls the optimistic UI
 *      change back to the previous posture (no silent UI/server drift).
 *
 * Both tests run against the live backend when reachable; they skip when
 * the dev backend is not up (mirrors anatomy.spec.ts pattern).
 *
 * Test 1 dispatches `mode_changed` directly via the dispatch store (no real
 * mode change on the backend) and asserts the resulting /api/tools refetch.
 * This avoids leaving the backend in `headless` after the test runs.
 */

import { test, expect } from './fixtures/backend';

test.describe('phase-14 settings — tools refetch + rollback', () => {
  test('mode_changed triggers /api/tools refetch', async ({ page, backendReady }) => {
    expect(backendReady).toBe(true);

    let toolsRequests = 0;
    await page.route('**/api/tools', async (route) => {
      toolsRequests += 1;
      await route.continue();
    });

    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');

    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.getByText('Security mode')).toBeVisible();

    // Expand the Tools section so the initial fetch fires
    const toolsToggle = page.getByRole('button', { name: /^Tools/ });
    await toolsToggle.click();
    await page.waitForFunction(() => {
      const rows = document.querySelectorAll('.tool-table__row');
      return rows.length > 0;
    }, undefined, { timeout: 5_000 });

    const before = toolsRequests;
    expect(before).toBeGreaterThan(0);

    // Inject mode_changed via dispatch — no backend mutation, just the
    // envelope that the WS would normally deliver.
    await page.evaluate(async () => {
      const { handleEnvelope } = await import('/src/stores/dispatch.ts');
      handleEnvelope({
        type: 'mode_changed',
        category: 'routing',
        session_id: 'e2e',
        timestamp: new Date().toISOString().replace('Z', ''),
        data: { from: 'max', to: 'headless' },
      });
    });

    // The route counter is the ground truth for whether a refetch happened.
    await expect.poll(() => toolsRequests, { timeout: 3_000 }).toBeGreaterThan(before);
  });

  test('failing tool-permission update rolls back optimistic state', async ({ page, backendReady }) => {
    expect(backendReady).toBe(true);

    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    await page.getByRole('button', { name: 'Settings' }).click();
    await expect(page.getByText('Security mode')).toBeVisible();

    const toolsToggle = page.getByRole('button', { name: /^Tools/ });
    await toolsToggle.click();
    await page.waitForFunction(
      () => document.querySelectorAll('.tool-table__row').length > 0,
      undefined,
      { timeout: 5_000 },
    );

    // Pick the first tool row and read its current posture.
    const firstRow = page.locator('.tool-table__row').first();
    const select = firstRow.locator('select.tool-row__select');
    const before = await select.inputValue();

    // Choose a different value than `before` — flipping among auto/ask/deny.
    const next = before === 'ask' ? 'deny' : 'ask';

    // Force the backend to reject the next tool-permission write.
    await page.route('**/api/settings/tool-permission', async (route) => {
      await route.fulfill({
        status: 400,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'forced rejection (e2e)' }),
      });
    });

    await select.selectOption(next);

    // Optimistic value should snap back to `before` once the request fails.
    await expect.poll(async () => await select.inputValue(), { timeout: 3_000 }).toBe(before);

    // Error surface visible.
    await expect(page.getByText(/tool-permission|forced rejection/i)).toBeVisible({ timeout: 3_000 });
  });
});
