import { test, expect } from '@playwright/test';
import type { Page, Route } from '@playwright/test';

// MO-9-11 — Channels Status pane happy paths. Backend is not required:
// every `/api/channels/...` call is mocked via ``page.route()`` so the
// tests exercise the React store / dispatch wiring and the Restart
// button + Offline toggle UI. Pattern mirrors ``brief-tab.spec.ts``.

const _CHANNEL_TELEGRAM = {
  name: 'telegram',
  status_snapshot: {
    name: 'telegram',
    bridge_state: 'running',
    last_poll_at: '2026-05-14T08:00:00+00:00',
    error_count_24h: 0,
    messages_in_24h: 3,
    messages_out_24h: 2,
    pending_count: 0,
    allowed_count: 1,
  },
  extras: { override: null },
};

async function _mockChannelsList(
  page: Page,
  payload: { channels: typeof _CHANNEL_TELEGRAM[] } = {
    channels: [_CHANNEL_TELEGRAM],
  },
): Promise<void> {
  await page.route('**/api/channels', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(payload),
    });
  });
}

async function _activateChannelsTab(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Channels', exact: true }).click();
}

async function _seedSessionId(page: Page, id: string): Promise<void> {
  await page.evaluate((sid) => {
    const w = window as unknown as {
      __tesseractTestStores?: {
        websocket?: { setState: (patch: { sessionId: string }) => void };
      };
    };
    w.__tesseractTestStores?.websocket?.setState({ sessionId: sid });
  }, id);
}

test.describe('MO-9-11 Channels tab — Status pane', () => {
  test('renders the registered Telegram channel with status snapshot', async ({
    page,
  }) => {
    await _mockChannelsList(page);

    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    await _activateChannelsTab(page);

    await expect(page.getByTestId('channels-view')).toBeVisible();
    await expect(page.getByTestId('channels-rail-telegram')).toBeVisible();
    await expect(page.getByTestId('channel-status-pane')).toBeVisible();
    await expect(page.getByTestId('channel-state-badge')).toHaveText('running');
    // The Offline toggle defaults to "follow bridge state" (override null);
    // the segment's `is-active` class is the load-bearing assertion.
    await expect(page.getByTestId('channel-override-follow')).toHaveClass(
      /is-active/,
    );
  });

  test('restart button posts to /restart with the session_id', async ({
    page,
  }) => {
    let restartCalls = 0;
    let restartBody: { session_id?: string } = {};
    await _mockChannelsList(page);
    await page.route(
      '**/api/channels/telegram/restart',
      async (route: Route) => {
        restartCalls += 1;
        try {
          restartBody = JSON.parse(route.request().postData() || '{}');
        } catch {
          restartBody = {};
        }
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            status: 'approved',
            output: 'telegram restarted',
            channel: _CHANNEL_TELEGRAM,
          }),
        });
      },
    );

    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    await _activateChannelsTab(page);
    await _seedSessionId(page, 'mo-9-11-restart');

    await page.getByTestId('channel-restart-btn').click();
    await expect.poll(() => restartCalls).toBe(1);
    expect(restartBody.session_id).toBe('mo-9-11-restart');
  });

  test('offline toggle round-trips through POST /telegram/status', async ({
    page,
  }) => {
    let statusCalls = 0;
    let statusBody: { session_id?: string; override?: string | null } = {};
    await _mockChannelsList(page);
    await page.route(
      '**/api/channels/telegram/status',
      async (route: Route) => {
        statusCalls += 1;
        try {
          statusBody = JSON.parse(route.request().postData() || '{}');
        } catch {
          statusBody = {};
        }
        // The route fans the new override back onto the channel row so
        // the StatusPane's `is-active` segment flips without a follow-up
        // GET. Mirror the contract exactly.
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            status: 'approved',
            output: "telegram override set to 'offline'",
            channel: {
              ..._CHANNEL_TELEGRAM,
              extras: { override: 'offline' },
            },
          }),
        });
      },
    );

    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    await _activateChannelsTab(page);
    await _seedSessionId(page, 'mo-9-11-status');

    await page.getByTestId('channel-override-offline').click();
    await expect.poll(() => statusCalls).toBe(1);
    expect(statusBody.session_id).toBe('mo-9-11-status');
    expect(statusBody.override).toBe('offline');

    // After the round-trip, the Offline segment is the live `is-active`
    // — the load-bearing visual signal the operator relies on.
    await expect(page.getByTestId('channel-override-offline')).toHaveClass(
      /is-active/,
    );
  });
});
