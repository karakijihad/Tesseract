import { test as base } from '@playwright/test';

export const BACKEND_HEALTH_URL = 'http://localhost:8000/api/health';

export const test = base.extend<{ backendReady: true }>({
  backendReady: async ({ request }, use, testInfo) => {
    try {
      const resp = await request.get(BACKEND_HEALTH_URL, { timeout: 2_000 });
      if (!resp.ok()) {
        testInfo.skip(true, `backend not healthy: ${resp.status()}`);
      }
    } catch (err) {
      testInfo.skip(true, `backend unreachable at ${BACKEND_HEALTH_URL}: ${err}`);
    }
    await use(true);
  },
});

export { expect } from '@playwright/test';
