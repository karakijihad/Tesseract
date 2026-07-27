import { test, expect } from './fixtures/backend';

// F3 covers pulse coalescing, observations persist + fires_total,
// ObserverSection header relabel, and delta chip. Store-level asserts
// run via `page.evaluate` + dynamic `handleEnvelope` import (same
// pattern as anatomy.spec.ts) — no backend required for those.

const NOW = () => new Date().toISOString().replace('Z', '');

test.describe('phase-f3 alive-feel', () => {
  test('pulse coalesces 50 same-turn stream_text into one row', async ({ page, backendReady }) => {
    expect(backendReady).toBe(true);

    const consoleErrors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });

    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');

    const rowCount = await page.evaluate(async () => {
      const { handleEnvelope } = await import('/src/stores/dispatch.ts');
      const { usePulseStore } = await import('/src/stores/pulse.ts');
      usePulseStore.getState().clear();
      const ts = new Date().toISOString().replace('Z', '');
      handleEnvelope({
        type: 'stream_start',
        category: 'loop',
        session_id: 'e2e',
        timestamp: ts,
        data: { turn_id: 'T1' },
      });
      for (let i = 0; i < 50; i++) {
        handleEnvelope({
          type: 'stream_text',
          category: 'loop',
          session_id: 'e2e',
          timestamp: ts,
          data: { delta: `a${i}` },
        } as never);
      }
      const entries = usePulseStore.getState().entries;
      const coalesced = entries.filter((e) => e.turn_id === 'T1');
      return {
        coalescedCount: coalesced.length,
        deltaCount: coalesced[0]?.delta_count ?? 0,
        total: entries.length,
      };
    });

    expect(rowCount.coalescedCount).toBe(1);
    expect(rowCount.deltaCount).toBe(50);
    expect(consoleErrors).toEqual([]);
  });

  test('pulse separates two turns into two coalesced rows', async ({ page, backendReady }) => {
    expect(backendReady).toBe(true);
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');

    const counts = await page.evaluate(async () => {
      const { handleEnvelope } = await import('/src/stores/dispatch.ts');
      const { usePulseStore } = await import('/src/stores/pulse.ts');
      usePulseStore.getState().clear();
      const ts = new Date().toISOString().replace('Z', '');
      const pushTurn = (id: string) => {
        handleEnvelope({
          type: 'stream_start',
          category: 'loop',
          session_id: 'e2e',
          timestamp: ts,
          data: { turn_id: id },
        });
        for (let i = 0; i < 20; i++) {
          handleEnvelope({
            type: 'stream_text',
            category: 'loop',
            session_id: 'e2e',
            timestamp: ts,
            data: { delta: 'x' },
          } as never);
        }
      };
      pushTurn('T1');
      pushTurn('T2');
      const ids = usePulseStore
        .getState()
        .entries.filter((e) => e.turn_id)
        .map((e) => e.turn_id);
      return { coalescedTurnIds: ids };
    });

    expect(counts.coalescedTurnIds).toContain('T1');
    expect(counts.coalescedTurnIds).toContain('T2');
    expect(counts.coalescedTurnIds.filter((id) => id === 'T1').length).toBe(1);
    expect(counts.coalescedTurnIds.filter((id) => id === 'T2').length).toBe(1);
  });

  test('observer_result increments fires_total and stamps last_fire_ts', async ({ page, backendReady }) => {
    expect(backendReady).toBe(true);
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');

    const result = await page.evaluate(async () => {
      localStorage.removeItem('tars-observations');
      const { handleEnvelope } = await import('/src/stores/dispatch.ts');
      const { useObservationsStore } = await import('/src/stores/observations.ts');
      useObservationsStore.getState().reset();
      const before = useObservationsStore.getState().fires_total;
      handleEnvelope({
        type: 'observer_result',
        category: 'background',
        session_id: 'e2e',
        timestamp: new Date().toISOString().replace('Z', ''),
        data: { mode: 'meta', observation: 'probe fire' },
      });
      const s = useObservationsStore.getState();
      return {
        fires_total: s.fires_total,
        before,
        last_fire_ts: s.observations[0]?.last_fire_ts ?? null,
        stored: s.observations.length,
      };
    });

    expect(result.before).toBe(0);
    expect(result.fires_total).toBe(1);
    expect(result.stored).toBe(1);
    expect(typeof result.last_fire_ts).toBe('number');
    expect(result.last_fire_ts).toBeGreaterThan(Date.now() - 10_000);
  });

  test('observations persist across reload (localStorage key tars-observations)', async ({ page, backendReady }) => {
    expect(backendReady).toBe(true);
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');

    await page.evaluate(async () => {
      const { handleEnvelope } = await import('/src/stores/dispatch.ts');
      const { useObservationsStore } = await import('/src/stores/observations.ts');
      useObservationsStore.getState().reset();
      handleEnvelope({
        type: 'observer_result',
        category: 'background',
        session_id: 'e2e',
        timestamp: new Date().toISOString().replace('Z', ''),
        data: { mode: 'meta', observation: 'persisted entry' },
      });
    });

    await page.reload();
    await page.waitForLoadState('domcontentloaded');

    const storedAfterReload = await page.evaluate(async () => {
      const { useObservationsStore } = await import('/src/stores/observations.ts');
      const raw = localStorage.getItem('tars-observations');
      return {
        storeLen: useObservationsStore.getState().observations.length,
        storeFiresTotal: useObservationsStore.getState().fires_total,
        localStorageKey: raw != null,
      };
    });

    expect(storedAfterReload.localStorageKey).toBe(true);
    expect(storedAfterReload.storeLen).toBeGreaterThan(0);
    expect(storedAfterReload.storeFiresTotal).toBeGreaterThan(0);
  });

  test('ObserverSection header renders "{n} stored · {fires_total} total fires"', async ({ page, backendReady }) => {
    expect(backendReady).toBe(true);
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');

    await page.evaluate(async () => {
      const { handleEnvelope } = await import('/src/stores/dispatch.ts');
      const { useObservationsStore } = await import('/src/stores/observations.ts');
      useObservationsStore.getState().reset();
      handleEnvelope({
        type: 'observer_result',
        category: 'background',
        session_id: 'e2e',
        timestamp: new Date().toISOString().replace('Z', ''),
        data: { mode: 'meta', observation: 'header probe' },
      });
    });

    const headerText = await page.locator('.obs-header-count').first().innerText();
    expect(headerText).toMatch(/\d+ stored · \d+ total fires/);
  });

  test('delta chip reads "last fire Xs ago" after observer_result', async ({ page, backendReady }) => {
    expect(backendReady).toBe(true);
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');

    await page.evaluate(async () => {
      const { handleEnvelope } = await import('/src/stores/dispatch.ts');
      const { useObservationsStore } = await import('/src/stores/observations.ts');
      useObservationsStore.getState().reset();
      handleEnvelope({
        type: 'observer_result',
        category: 'background',
        session_id: 'e2e',
        timestamp: new Date().toISOString().replace('Z', ''),
        data: { mode: 'meta', observation: 'chip probe' },
      });
    });

    // The panel is collapsed by default — expand it so rows render.
    const toggle = page.getByRole('button', { name: /Observer/ });
    if (await toggle.count()) {
      await toggle.first().click();
    }

    const chip = page.locator('.obs-delta-chip').first();
    await expect(chip).toBeVisible();
    const chipText = await chip.innerText();
    expect(chipText).toMatch(/last fire \d+s ago/i);
  });

  test('terminal keepAlive: DOM node persists across tab switches (D11)', async ({ page, backendReady }) => {
    expect(backendReady).toBe(true);
    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');

    const terminalPane = page.locator('.view-pane').nth(2);
    const terminalRoot = await terminalPane.elementHandle();
    expect(terminalRoot).not.toBeNull();

    // Switch to Pulse tab, then back. The DOM node for TerminalView should
    // be the same element (never unmounted) — `.is-active` just toggles.
    const pulseBtn = page.getByRole('button', { name: /Pulse/ }).first();
    if (await pulseBtn.count()) await pulseBtn.click();
    const terminalBtn = page.getByRole('button', { name: /Terminal/ }).first();
    if (await terminalBtn.count()) await terminalBtn.click();

    const afterRoot = await terminalPane.elementHandle();
    const same = await page.evaluate(
      ([a, b]) => a === b,
      [terminalRoot, afterRoot],
    );
    expect(same).toBe(true);
  });
});
