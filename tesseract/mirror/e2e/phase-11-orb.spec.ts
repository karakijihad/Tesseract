import { test, expect } from '@playwright/test';

const AUDIT_DIR = '../../Docs/Plan/mirror/audits/phase-11-2026-04-19';

test.describe('phase 11 — orb as body of TARS', () => {
  test.beforeEach(async ({ page }) => {
    page.on('console', (msg) => {
      if (msg.type() === 'error') {
        throw new Error(`Console error: ${msg.text()}`);
      }
    });
  });

  test('mood bridge end-to-end: pump cadence + ingestBackend wired', async ({ page }) => {
    const wsEntitySignals: { mood_intensity: number; mood_valence: number; ts: number }[] = [];
    page.on('websocket', (ws) => {
      ws.on('framereceived', ({ payload }) => {
        try {
          const env = JSON.parse(payload as string);
          if (env.type === 'entity_signals') {
            wsEntitySignals.push({
              mood_intensity: env.data.mood_intensity,
              mood_valence: env.data.mood_valence,
              ts: Date.now(),
            });
          }
        } catch { /* ignore */ }
      });
    });

    await page.goto('/');
    await page.waitForSelector('canvas.global-canvas', { timeout: 10_000 });
    await page.waitForTimeout(5_500);
    await page.screenshot({ path: `${AUDIT_DIR}/01-orb-default-mood.png`, fullPage: false });

    expect(wsEntitySignals.length).toBeGreaterThanOrEqual(2);
    for (let i = 1; i < wsEntitySignals.length; i++) {
      const delta = wsEntitySignals[i].ts - wsEntitySignals[i - 1].ts;
      expect(delta).toBeGreaterThan(1500);
      expect(delta).toBeLessThan(2700);
    }
    expect(wsEntitySignals[0].mood_intensity).toBeCloseTo(0.5, 2);
    expect(wsEntitySignals[0].mood_valence).toBeCloseTo(0.0, 2);
  });

  test('valence shifts orb hue + brightness (no add-on layers)', async ({ page }) => {
    await page.goto('/');
    await page.waitForSelector('canvas.global-canvas');
    await page.waitForTimeout(2_000);

    // Inject positive valence + read getValence in the same evaluate so the
    // WS pump (every ~2s) can't overwrite between inject and read.
    const positiveValence = await page.evaluate(() => {
      const ctrl = (window as { __entityController?: { getSignals: () => { ingestBackend: (p: object) => void; getValence: () => number } } }).__entityController;
      ctrl?.getSignals().ingestBackend({ mood_intensity: 0.85, mood_valence: 0.9 });
      return ctrl?.getSignals().getValence();
    });
    expect(positiveValence).toBeCloseTo(0.9, 2);
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${AUDIT_DIR}/02-orb-positive-valence-bright.png` });

    // Flip to negative valence — orb should desaturate and dim.
    await page.evaluate(() => {
      const ctrl = (window as { __entityController?: { getSignals: () => { ingestBackend: (p: object) => void } } }).__entityController;
      ctrl?.getSignals().ingestBackend({ mood_intensity: 0.3, mood_valence: -0.8 });
    });
    await page.waitForTimeout(400);
    await page.screenshot({ path: `${AUDIT_DIR}/03-orb-negative-valence-subdued.png` });
  });

  test('pulseEvent reaction pops — success / error / user', async ({ page }) => {
    await page.goto('/');
    await page.waitForSelector('canvas.global-canvas');
    await page.waitForTimeout(1_500);

    // Error pop — sharp red flash + bigger amplitude.
    await page.evaluate(() => {
      const ctrl = (window as { __entityController?: { pulseEvent: (k: string) => void } }).__entityController;
      ctrl?.pulseEvent('error');
    });
    await page.waitForTimeout(120);
    await page.screenshot({ path: `${AUDIT_DIR}/04-pop-error.png` });
    await page.waitForTimeout(500);

    // Success pop — accent-bright glow.
    await page.evaluate(() => {
      const ctrl = (window as { __entityController?: { pulseEvent: (k: string) => void } }).__entityController;
      ctrl?.pulseEvent('success');
    });
    await page.waitForTimeout(120);
    await page.screenshot({ path: `${AUDIT_DIR}/05-pop-success.png` });
    await page.waitForTimeout(500);

    // User pop — small accent bloom.
    await page.evaluate(() => {
      const ctrl = (window as { __entityController?: { pulseEvent: (k: string) => void } }).__entityController;
      ctrl?.pulseEvent('user');
    });
    await page.waitForTimeout(80);
    await page.screenshot({ path: `${AUDIT_DIR}/06-pop-user.png` });
  });

  test('thinking state triggers wobble', async ({ page }) => {
    await page.goto('/');
    await page.waitForSelector('canvas.global-canvas');
    await page.waitForTimeout(1_500);

    // Force into 'thinking' via store.
    await page.evaluate(() => {
      // Vite import via dynamic ES module is complex; use the store's window key.
      const w = window as { __zustandStores?: { entity?: { getState: () => { setState: (s: string) => void } } } };
      // Fallback: use the registry via __entityController which subscribes to the store.
      // Easier path: mutate via the store directly by reaching into the import.
      // The store is created at module scope; we rely on the controller seeing it via subscription.
      // So: use eval to set state through the global if exposed; otherwise post a DOM event.
      // Simplest: trigger via a chat send simulation by setting state via the controller's signals onStep
      // and then setting entity state via a CustomEvent the controller listens to.
      // Pragmatic shortcut — directly set state through a dispatched DOM event interpreted by a test bridge,
      // OR use the dispatch import via a script tag injection. For now: rely on the store being accessible
      // via the React DevTools-exposed __REACT_DEVTOOLS_GLOBAL_HOOK__ — too brittle.
      void w; // mark used
    });
    // Pragmatic alternative: directly poke the controller's _wobbleAmp via a dev-only helper.
    // We expose nothing for that today, so we trigger thinking via the store using the global module link.
    await page.evaluate(async () => {
      const mod = await import('/src/stores/entity.ts');
      // @ts-expect-error dynamic import
      mod.useEntityStore.getState().setState('thinking');
    });
    await page.waitForTimeout(2_500); // let wobbleAmp lerp toward 1
    await page.screenshot({ path: `${AUDIT_DIR}/07-thinking-wobble.png` });

    await page.evaluate(async () => {
      const mod = await import('/src/stores/entity.ts');
      // @ts-expect-error dynamic import
      mod.useEntityStore.getState().setState('idle');
    });
    await page.waitForTimeout(1_500);
    await page.screenshot({ path: `${AUDIT_DIR}/08-idle-no-wobble.png` });
  });
});
