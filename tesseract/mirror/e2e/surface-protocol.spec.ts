// Y-2 — Surface Protocol e2e.
//
// Verifies the three done-criteria that need a live stack:
//   1. A surface.create(folder) → folder card appears on the canvas. The
//      REST create endpoint is the same SurfaceStore path the
//      `surface_create` kernel tool drives, and the card lands via the
//      `surface` WS channel (tool → bus → ws pump → store → render).
//   2. The operator drags the card → surface.emit_event(moved) fires and
//      the backend persists the new geometry.
//   3. Reload → the card re-renders at the new position (REST hydrate).
//
// Skips cleanly if the backend at localhost:8000 isn't running.
// Screenshots land under Docs/Plan/tars-cockpit/audits/Y-2/.

import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

const SHOT_DIR = '../../Docs/Plan/tars-cockpit/audits/Y-2';
// Node-level (APIRequestContext) calls target 127.0.0.1 explicitly — the
// backend binds 127.0.0.1 (mirror.yaml) and Playwright's Node request
// resolves bare `localhost` to ::1 first, which isn't bound. The browser
// page itself reaches the backend via the app's `localhost:8000` config
// (Chrome resolves to 127.0.0.1), so only these requests need the literal.
const BACKEND = 'http://127.0.0.1:8000';

// Self-contained readiness gate (replaces the shared backend fixture so the
// health probe also uses 127.0.0.1). Skips the suite cleanly if the backend
// isn't up.
let backendUp = false;
test.beforeAll(async ({ request }) => {
  try {
    const resp = await request.get(`${BACKEND}/api/health`, { timeout: 5_000 });
    backendUp = resp.ok();
  } catch {
    backendUp = false;
  }
});
test.beforeEach(async ({ request }) => {
  test.skip(!backendUp, `backend unreachable at ${BACKEND}/api/health`);
  // Start each test from a clean tars canvas — the live backend persists to
  // the operator's real workspace canvas-state, so close any leftover
  // surfaces (from a prior run) before creating fresh ones.
  const resp = await request.get(`${BACKEND}/api/surfaces/tars`);
  if (resp.ok()) {
    const { surfaces } = await resp.json();
    for (const s of surfaces) {
      await request.post(`${BACKEND}/api/surfaces/tars/event`, {
        data: { surface_id: s.id, event: 'closed', detail: {} },
      });
    }
  }
});

async function openTars(page: Page): Promise<void> {
  await page.goto('/');
  await page.waitForLoadState('domcontentloaded');
  await page.waitForFunction(() => Boolean((window as any).__tesseractTestStores));
  await page.evaluate(() => {
    (window as any).__tesseractTestStores.ui.getState().setView('tars');
  });
  await page.locator('[data-testid="canvas-tars"] .tl-container').waitFor({
    state: 'visible',
    timeout: 15_000,
  });
}

async function createFolderSurface(page: Page): Promise<string> {
  const resp = await page.request.post(`${BACKEND}/api/surfaces/tars`, {
    data: {
      type: 'folder',
      title: 'E2E Folder',
      position: { x: 120, y: 120 },
      props: { root: 'C:/tesseract/demo', entries: ['alpha.txt', { name: 'sub', kind: 'dir' }] },
    },
  });
  expect(resp.ok()).toBe(true);
  return (await resp.json()).surface_id as string;
}

async function backendPosition(page: Page, sid: string): Promise<{ x: number; y: number }> {
  const resp = await page.request.get(`${BACKEND}/api/surfaces/tars`);
  const body = await resp.json();
  const s = body.surfaces.find((d: any) => d.id === sid);
  return s.position;
}

test.describe('Y-2 — Surface Protocol', () => {
  test('tool create → folder card appears on the canvas', async ({ page }) => {
    await openTars(page);

    const sid = await createFolderSurface(page);
    const card = page.locator(`[data-surface-id="${sid}"]`);
    await card.waitFor({ state: 'visible', timeout: 10_000 });

    await expect(card).toContainText('C:/tesseract/demo');
    await expect(card).toContainText('alpha.txt');
    await page.screenshot({ path: `${SHOT_DIR}/folder-card-appears.png` });
  });

  test('drag emits moved + reload re-renders at the new position', async ({ page }) => {
    await openTars(page);

    const sid = await createFolderSurface(page);
    const card = page.locator(`[data-surface-id="${sid}"]`);
    await card.waitFor({ state: 'visible', timeout: 10_000 });

    const before = await backendPosition(page, sid);

    // Drag the title bar by a deterministic offset.
    const bar = card.locator('.surface-card__bar');
    const box = await bar.boundingBox();
    if (!box) throw new Error('no bar bounding box');
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width / 2 + 180, box.y + box.height / 2 + 140, { steps: 8 });
    await page.mouse.up();

    // emit_event(moved) is fire-and-forget (with a CORS preflight), so poll
    // the backend until the new geometry has persisted.
    await expect
      .poll(async () => (await backendPosition(page, sid)).x, { timeout: 5_000 })
      .toBeGreaterThan(before.x + 100);
    const after = await backendPosition(page, sid);
    expect(after.y).toBeGreaterThan(before.y + 100);
    await page.screenshot({ path: `${SHOT_DIR}/dragged-card.png` });

    // Reload: the card hydrates from REST at the persisted (moved) position.
    await openTars(page);
    const reloaded = page.locator(`[data-surface-id="${sid}"]`);
    await reloaded.waitFor({ state: 'visible', timeout: 10_000 });
    const style = await reloaded.evaluate((el) => ({
      left: (el as HTMLElement).style.left,
      top: (el as HTMLElement).style.top,
    }));
    expect(parseFloat(style.left)).toBeGreaterThan(before.x + 100);
    expect(parseFloat(style.top)).toBeGreaterThan(before.y + 100);
    await page.screenshot({ path: `${SHOT_DIR}/reload-persisted.png` });
  });

  test('unknown surface type renders the JSON-dump fallback (no crash)', async ({ page }) => {
    await openTars(page);

    const resp = await page.request.post(`${BACKEND}/api/surfaces/tars`, {
      data: { type: 'mystery-type', title: 'Unknown', props: { foo: 'bar' } },
    });
    const sid = (await resp.json()).surface_id as string;
    const card = page.locator(`[data-surface-id="${sid}"]`);
    await card.waitFor({ state: 'visible', timeout: 10_000 });
    await expect(card).toContainText('unknown type: mystery-type');
    await page.screenshot({ path: `${SHOT_DIR}/unknown-type-fallback.png` });
  });
});
