// CV-1 — the trio: Claude/Codex live-lane cards on the canvas.
//
// Live stack required: Mirror backend (8000) + controller daemon (which the
// supervisor spawns) + an authenticated `claude` CLI. Skips cleanly if the
// controller daemon is down (GET /api/lanes/named → 503).
//
// Verifies:
//   1. Spawn trio → two lane cards (claude left, codex right) + routing card.
//   2. Send a follow-up to coder/claude → Claude responds, events stream in.
//   3. Reload → the card re-attaches to the live lane (re-attach indicator),
//      proving the lane survives independent of the page (P-3).
//
// Screenshots land under Docs/Plan/tars-cockpit/audits/CV-1/.

import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

const SHOT_DIR = '../../Docs/Plan/tars-cockpit/audits/CV-1';
const BACKEND = 'http://127.0.0.1:8000';

let controllerUp = false;
test.beforeAll(async ({ request }) => {
  try {
    const health = await request.get(`${BACKEND}/api/health`, { timeout: 5_000 });
    if (!health.ok()) return;
    // /api/lanes/named is 200 only when the controller daemon answers.
    const named = await request.get(`${BACKEND}/api/lanes/named`, { timeout: 8_000 });
    controllerUp = named.ok();
  } catch {
    controllerUp = false;
  }
});
test.beforeEach(async ({ request }) => {
  test.skip(!controllerUp, `controller daemon unreachable (GET ${BACKEND}/api/lanes/named not ok)`);
  // Clear the tars canvas so each test starts from one fresh trio (the live
  // backend persists surfaces to the operator's real canvas; auto-spawn +
  // prior runs would otherwise accumulate duplicate cards).
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
  await page.evaluate(() => (window as any).__tesseractTestStores.ui.getState().setView('tars'));
  await page.locator('[data-testid="canvas-tars"] .tl-container').waitFor({ state: 'visible', timeout: 15_000 });
}

async function spawnTrio(page: Page): Promise<void> {
  // TrioLauncher auto-spawns on mount (named lanes exist); the click is the
  // idempotent manual path. spawnTrio() is in-flight-guarded so the two can't
  // double-create. Wait until exactly the two lane cards are present.
  await page.locator('[data-testid="spawn-trio"]').click().catch(() => undefined);
  await expect(page.locator('[data-surface-type="lane"]')).toHaveCount(2, { timeout: 40_000 });
}

test.describe('CV-1 — Claude/Codex trio', () => {
  test('spawn trio renders Claude + Codex lane cards + routing applet', async ({ page }) => {
    await openTars(page);
    await spawnTrio(page);

    await expect(page.locator('[data-lane-kind="claude"]')).toBeVisible();
    await expect(page.locator('[data-lane-kind="codex"]')).toBeVisible();
    await expect(page.locator('.trio-routing')).toContainText('TARS');
    await page.screenshot({ path: `${SHOT_DIR}/trio-spawned.png` });
  });

  test('follow-up to coder/claude streams live events into the card', async ({ page }) => {
    test.setTimeout(120_000); // a real Claude turn
    await openTars(page);
    await spawnTrio(page);

    const claude = page.locator('[data-lane-kind="claude"]');
    await claude.locator('.lane-card__draft').fill('Reply with exactly the single word: PONG');
    await claude.locator('.lane-card__send').click();

    // The operator → card → lane → claude CLI → events → card loop: assistant
    // text streams back into the card. We assert an assistant_text event
    // appears (not specific content) so the convergence is verified even when
    // the live Claude account can't emit a model answer (e.g. out of credit).
    await expect(claude.locator('.lane-ev--text')).toBeVisible({ timeout: 90_000 });
    await page.screenshot({ path: `${SHOT_DIR}/claude-responded.png` });
  });

  test('reload re-attaches the card to the surviving lane (P-3)', async ({ page }) => {
    test.setTimeout(120_000);
    await openTars(page);
    await spawnTrio(page);

    // Give the lane some history so the re-attach replay is non-empty.
    const claude = page.locator('[data-lane-kind="claude"]');
    await claude.locator('.lane-card__draft').fill('Reply with exactly: ACK');
    await claude.locator('.lane-card__send').click();
    await expect(claude.locator('.lane-ev--text')).toBeVisible({ timeout: 90_000 });

    // Reload — the card re-mounts and re-attaches to the (surviving) lane.
    await openTars(page);
    const reattached = page.locator('[data-lane-kind="claude"] .lane-card__reattach');
    await reattached.waitFor({ state: 'visible', timeout: 20_000 });
    await expect(reattached).toContainText('re-attached after brain restart');
    await page.screenshot({ path: `${SHOT_DIR}/reattach-after-restart.png` });
  });
});
