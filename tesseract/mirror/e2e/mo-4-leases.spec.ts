import { test, expect } from './fixtures/backend';

// MO-4 — REST + UI smoke. The full lease lifecycle (acquire/run/release) is
// covered by the 33 pytest cases in
// `tesseract/tests/fix_pass_mission_orchestrator_MO_4/`. This spec covers the
// thin Mirror surface that backend-tests cannot reach: that the new GET
// /api/missions/{id}/leases route returns the empty-shape on a fresh mission
// and that the Missions board still renders cleanly with the MO-4 type +
// CSS extensions.

const API_BASE = 'http://localhost:8000';

test.describe('MO-4 lease REST surface', () => {
  test('GET /api/missions/{id}/leases returns empty for a new mission', async ({
    request,
    backendReady,
  }) => {
    expect(backendReady).toBe(true);

    const create = await request.post(`${API_BASE}/api/missions`, {
      data: {
        objective: 'MO-4 lease probe',
        session_id: 'mo-4-spec',
        title: 'MO-4 lease probe',
        priority: 1,
      },
    });
    expect(create.ok()).toBe(true);
    const { id } = await create.json();

    const leases = await request.get(`${API_BASE}/api/missions/${id}/leases`);
    expect(leases.ok()).toBe(true);
    const body = await leases.json();
    expect(body).toEqual({ leases: [] });
  });

  test('GET /api/missions/{unknown}/leases returns 404', async ({
    request,
    backendReady,
  }) => {
    expect(backendReady).toBe(true);
    const res = await request.get(`${API_BASE}/api/missions/no-such-mission/leases`);
    expect(res.status()).toBe(404);
  });

  test('Missions board renders with MO-4 step extensions', async ({ page, backendReady }) => {
    expect(backendReady).toBe(true);
    const consoleErrors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    await page.getByRole('button', { name: 'Missions', exact: true }).click();
    await expect(page.locator('.missions-view-head')).toBeVisible();
    expect(consoleErrors.filter((m) => !m.includes('mission-events'))).toEqual([]);
  });
});
