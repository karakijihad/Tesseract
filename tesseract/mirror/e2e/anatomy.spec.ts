import { test, expect } from './fixtures/backend';
import type { KernelStage } from '../src/lib/anatomy/types';

interface AnatomyDevState {
  activeStage: KernelStage | null;
  signalPath: KernelStage[];
  stageMeta: Record<KernelStage, { lastEnterTs: number | null; pending: boolean }>;
}

declare global {
  interface Window {
    __anatomyRenderer?: {
      getState: () => AnatomyDevState;
    };
  }
}

test.describe('phase-3 kernel anatomy', () => {
  test('mounts the anatomy canvas on entity view', async ({ page, backendReady }) => {
    expect(backendReady).toBe(true);

    const consoleErrors: string[] = [];
    page.on('console', (msg) => {
      if (msg.type() === 'error') consoleErrors.push(msg.text());
    });

    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');

    await page.getByRole('button', { name: 'Entity' }).click();

    await expect(page.getByTestId('entity-split')).toBeVisible();
    await expect(page.getByTestId('anatomy-view')).toBeVisible();

    await page.waitForFunction(() => !!window.__anatomyRenderer);

    const state = await page.evaluate(() => window.__anatomyRenderer?.getState());
    expect(state).toBeDefined();
    expect(state?.stageMeta.permission.pending).toBe(true);
    expect(state?.stageMeta.memory_store.pending).toBe(true);
    expect(state?.stageMeta.router.pending).toBe(false);

    expect(consoleErrors).toEqual([]);
  });

  test('ingests kernel_stage events into the store', async ({ page, backendReady }) => {
    expect(backendReady).toBe(true);

    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    await page.getByRole('button', { name: 'Entity' }).click();
    await expect(page.getByTestId('anatomy-view')).toBeVisible();
    await page.waitForFunction(() => !!window.__anatomyRenderer);

    await page.evaluate(async () => {
      const { handleEnvelope } = await import('/src/stores/dispatch.ts');
      const now = new Date().toISOString().replace('Z', '');
      for (const stage of ['triage', 'router', 'model_call', 'response'] as const) {
        handleEnvelope({
          type: 'kernel_stage',
          category: 'loop',
          session_id: 'e2e',
          timestamp: now,
          data: { stage, status: 'enter', meta: {} },
        });
      }
    });

    const state = await page.evaluate(() => window.__anatomyRenderer?.getState());
    expect(state?.activeStage).toBe('response');
    expect(state?.signalPath).toEqual(['triage', 'router', 'model_call', 'response']);
  });
});
