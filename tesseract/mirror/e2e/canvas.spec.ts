// Y-1 — canvas substrate e2e.
//
// Verifies the three Y-1 done-criteria that need a live stack:
//   1. The empty canvas mounts in TarsView with the orb visible.
//   2. A shape created on the canvas survives a full page reload
//      (per-view server persistence round-trips).
//   3. Per-view state is isolated — the persisted tars canvas carries the
//      shape while another view's canvas state stays empty (404).
//
// Skips cleanly if the backend at localhost:8000 isn't running.
// Screenshots land under Docs/Plan/tars-cockpit/audits/Y-1/.

import { test, expect } from './fixtures/backend';

const SHOT_DIR = '../../Docs/Plan/tars-cockpit/audits/Y-1';

async function openTars(page: import('@playwright/test').Page): Promise<void> {
  await page.goto('/');
  await page.waitForLoadState('domcontentloaded');
  await page.waitForFunction(() => Boolean((window as any).__tesseractTestStores));
  await page.evaluate(() => {
    (window as any).__tesseractTestStores.ui.getState().setView('tars');
  });
  // tldraw is lazy-loaded on first canvas activation; wait for its DOM.
  await page.locator('[data-testid="canvas-tars"] .tl-container').waitFor({
    state: 'visible',
    timeout: 15_000,
  });
}

async function waitForEditor(page: import('@playwright/test').Page): Promise<void> {
  await page.waitForFunction(
    () => Boolean((window as any).__tldrawEditors?.tars),
    undefined,
    { timeout: 15_000 },
  );
}

function shapeCount(page: import('@playwright/test').Page): Promise<number> {
  return page.evaluate(
    () => (window as any).__tldrawEditors.tars.getCurrentPageShapes().length as number,
  );
}

test.describe('Y-1 — canvas substrate', () => {
  test('empty canvas mounts in TarsView with the orb visible', async ({
    page,
    backendReady,
  }) => {
    expect(backendReady).toBe(true);

    const consoleErrors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() !== 'error') return;
      const text = msg.text();
      // The canvas e2e runs against a minimal backend that serves only the
      // canvas + health routes, so the app's unrelated boot fetches
      // (/api/system/*, /api/runtime/status, /ws, soul, slashCommands, …)
      // network-fail. Filter that harness noise — every variant is a
      // network/fetch/websocket failure; a genuine canvas/tldraw/render
      // error has none of these markers and still trips the assertion.
      if (
        /CORS policy|ERR_FAILED|Failed to load resource|net::|Failed to fetch|WebSocket connection/.test(
          text,
        )
      ) {
        return;
      }
      consoleErrors.push(text);
    });

    await openTars(page);
    await waitForEditor(page);

    // Start from a genuinely empty canvas for the screenshot (the backend
    // may carry shapes from a prior test in this run).
    await page.evaluate(() => {
      const ed = (window as any).__tldrawEditors.tars;
      ed.selectAll();
      ed.deleteShapes(ed.getSelectedShapeIds());
    });

    // The orb is the singleton WebGL canvas (GlobalCanvas) in full mode.
    await expect(page.locator('canvas.global-canvas.full')).toBeVisible();
    await expect(page.locator('[data-testid="canvas-tars"] .tl-container')).toBeVisible();

    await page.screenshot({ path: `${SHOT_DIR}/empty-canvas-tarsview.png` });
    expect(consoleErrors).toEqual([]);
  });

  test('a dragged shape persists across reload', async ({ page, backendReady }) => {
    expect(backendReady).toBe(true);

    await openTars(page);
    await waitForEditor(page);

    // Start from a clean canvas, then create one shape.
    await page.evaluate(() => {
      const ed = (window as any).__tldrawEditors.tars;
      ed.selectAll();
      ed.deleteShapes(ed.getSelectedShapeIds());
      ed.createShape({
        type: 'geo',
        x: 220,
        y: 180,
        props: { geo: 'rectangle', w: 160, h: 110 },
      });
    });
    expect(await shapeCount(page)).toBe(1);

    // Let the 1s debounce fire and the PUT land before reloading.
    await page.waitForTimeout(1500);
    await page.screenshot({ path: `${SHOT_DIR}/drag-persist.png` });

    await openTars(page);
    await waitForEditor(page);
    expect(await shapeCount(page)).toBe(1);
  });

  test('per-view state is isolated from other views', async ({ page, backendReady }) => {
    expect(backendReady).toBe(true);

    await openTars(page);
    await waitForEditor(page);

    await page.evaluate(() => {
      const ed = (window as any).__tldrawEditors.tars;
      ed.selectAll();
      ed.deleteShapes(ed.getSelectedShapeIds());
      ed.createShape({
        type: 'geo',
        x: 120,
        y: 120,
        props: { geo: 'ellipse', w: 90, h: 90 },
      });
    });
    await page.waitForTimeout(1500);

    // The tars canvas state carries the shape; a view that has never been
    // wrapped/opened has no persisted canvas state at all (404).
    const tars = await page.request.get('http://localhost:8000/api/canvas/tars');
    expect(tars.status()).toBe(200);
    const tarsBody = await tars.json();
    expect(tarsBody.tldraw_snapshot).toBeTruthy();

    const pulse = await page.request.get('http://localhost:8000/api/canvas/pulse');
    expect(pulse.status()).toBe(404);

    await page.screenshot({ path: `${SHOT_DIR}/per-view-isolation.png` });
  });
});
