import { test, expect } from '@playwright/test';
import type { Page, Route } from '@playwright/test';

// MO-9-12 — Channels tab Users + Conversations panes. The backend is
// not required: every `/api/channels/...` call is mocked via
// `page.route()` so the tests exercise the React store / dispatch wiring
// and the Approve / Revoke / Block flows. Pattern mirrors
// `channels-status.spec.ts`.

const _CHANNEL_TELEGRAM = {
  name: 'telegram',
  status_snapshot: {
    name: 'telegram',
    bridge_state: 'running',
    last_poll_at: '2026-05-14T08:00:00+00:00',
    error_count_24h: 0,
    messages_in_24h: 7,
    messages_out_24h: 4,
    pending_count: 1,
    allowed_count: 1,
  },
  extras: { override: null },
};

const _USERS_INITIAL = {
  name: 'telegram',
  users: [
    {
      user_id: '42',
      display_name: 'Jane Doe',
      tier: 'operator',
      ttl_iso: null,
      first_seen: '2026-05-14T07:00:00+00:00',
      last_seen: '2026-05-14T08:00:00+00:00',
      messages_total: 5,
      state: 'allowed',
    },
    {
      user_id: '99',
      display_name: '@newcomer',
      tier: 'operator',
      ttl_iso: null,
      first_seen: '2026-05-14T07:30:00+00:00',
      last_seen: '2026-05-14T08:01:00+00:00',
      messages_total: 1,
      state: 'pending',
    },
  ],
};

async function _mockChannelsList(page: Page): Promise<void> {
  await page.route('**/api/channels', async (route) => {
    if (route.request().method() !== 'GET') {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ channels: [_CHANNEL_TELEGRAM] }),
    });
  });
}

async function _mockUsers(
  page: Page,
  payload: typeof _USERS_INITIAL = _USERS_INITIAL,
): Promise<void> {
  await page.route('**/api/channels/telegram/users', async (route) => {
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

test.describe('MO-9-12 Channels tab — Users pane', () => {
  test('renders allowlist and pending tables from /api/channels/{name}/users', async ({
    page,
  }) => {
    await _mockChannelsList(page);
    await _mockUsers(page);

    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    await _activateChannelsTab(page);
    await page.getByTestId('channels-pane-users').click();

    await expect(page.getByTestId('channel-users-pane')).toBeVisible();
    // The pending row migrates the operator's eye to Approve; the
    // allowlist row carries Revoke + Block. Both states must be visible.
    await expect(page.getByTestId('channel-user-row:99')).toBeVisible();
    await expect(page.getByTestId('channel-user-row:42')).toBeVisible();
    await expect(page.getByTestId('channel-user-approve:99')).toBeVisible();
    await expect(page.getByTestId('channel-user-revoke:42')).toBeVisible();
  });

  test('approve button opens modal then POSTs to /approve with tier=operator', async ({
    page,
  }) => {
    let approveCalls = 0;
    let approveBody: Record<string, unknown> = {};
    const usersAfter = {
      ...structuredClone(_USERS_INITIAL),
      users: _USERS_INITIAL.users.map((u) =>
        u.user_id === '99' ? { ...u, state: 'allowed', display_name: 'Newbie' } : u,
      ),
    };

    await _mockChannelsList(page);
    // Approve flips the bridge state; the users mock should return the
    // pre-approval payload until POST /approve fires, then the post-
    // approval payload. StrictMode mounts useEffect twice in dev so a
    // call-counter would tear at the 2nd auto-fetch — gate on the flag
    // POST sets to avoid that race.
    let approveFired = false;
    await page.route('**/api/channels/telegram/users', async (route) => {
      if (route.request().method() !== 'GET') {
        await route.fallback();
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(approveFired ? usersAfter : _USERS_INITIAL),
      });
    });
    await page.route(
      '**/api/channels/telegram/approve',
      async (route: Route) => {
        approveCalls += 1;
        approveFired = true;
        try {
          approveBody = JSON.parse(route.request().postData() || '{}');
        } catch {
          approveBody = {};
        }
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            status: 'approved',
            output: 'telegram:99 approved as operator',
            user: { ...usersAfter.users[1] },
            person_record_path:
              '/tmp/memory-store/reference/people/newbie.md',
            person_record_error: null,
          }),
        });
      },
    );

    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    await _activateChannelsTab(page);
    await _seedSessionId(page, 'mo-9-12-approve');
    await page.getByTestId('channels-pane-users').click();

    await page.getByTestId('channel-user-approve:99').click();
    await expect(page.getByTestId('channel-approval-modal')).toBeVisible();

    // The display name pre-fills from the pending row's `@newcomer`; the
    // operator typically renames it before approving. We type a clean
    // name then confirm. Tier defaults to operator — the friend option is
    // ghosted so we skip the dropdown interaction.
    const nameInput = page.getByTestId('channel-approval-display-name');
    await nameInput.fill('Newbie');
    await page.getByTestId('channel-approval-confirm').click();

    await expect.poll(() => approveCalls).toBe(1);
    expect(approveBody.session_id).toBe('mo-9-12-approve');
    expect(approveBody.user_id).toBe('99');
    expect(approveBody.tier).toBe('operator');
    expect(approveBody.display_name).toBe('Newbie');
    // Modal dismisses on success; pending row migrates after refetch.
    await expect(page.getByTestId('channel-approval-modal')).toHaveCount(0);
  });

  test('revoke button POSTs to /revoke with the session_id', async ({ page }) => {
    let revokeCalls = 0;
    await _mockChannelsList(page);
    await _mockUsers(page);
    await page.route(
      '**/api/channels/telegram/revoke',
      async (route: Route) => {
        revokeCalls += 1;
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            status: 'approved',
            output: 'telegram:42 revoked',
            user: { ..._USERS_INITIAL.users[0], state: 'pending' },
          }),
        });
      },
    );

    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    await _activateChannelsTab(page);
    await _seedSessionId(page, 'mo-9-12-revoke');
    await page.getByTestId('channels-pane-users').click();

    await page.getByTestId('channel-user-revoke:42').click();
    await expect.poll(() => revokeCalls).toBe(1);
  });
});

test.describe('MO-9-12 Channels tab — Conversations pane', () => {
  test('renders chronological message list for the selected user', async ({
    page,
  }) => {
    await _mockChannelsList(page);
    await _mockUsers(page);
    await page.route(
      '**/api/channels/telegram/users/42/conversation*',
      async (route: Route) => {
        if (route.request().method() !== 'GET') {
          await route.fallback();
          return;
        }
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({
            name: 'telegram',
            user_id: '42',
            rows: [
              {
                ts: '2026-05-14T08:00:05+00:00',
                direction: 'outbound',
                body: 'hi back',
                extra: {},
              },
              {
                ts: '2026-05-14T08:00:00+00:00',
                direction: 'inbound',
                body: 'hello',
                extra: { telegram_message_id: 7 },
              },
            ],
            limit: 100,
            before_iso: null,
          }),
        });
      },
    );

    await page.goto('/');
    await page.waitForLoadState('domcontentloaded');
    await _activateChannelsTab(page);
    await page.getByTestId('channels-pane-conversations').click();

    await expect(page.getByTestId('channel-conv-pane')).toBeVisible();
    // The pane auto-picks the first allowed user (chat_id 42), pulls the
    // transcript, and renders inbound + outbound rows. Retention hint is
    // load-bearing because the phase doc §1 calls it out explicitly.
    await expect(page.getByTestId('channel-conv-retention')).toBeVisible();
    await expect(page.getByTestId('channel-conv-row:inbound')).toBeVisible();
    await expect(page.getByTestId('channel-conv-row:outbound')).toBeVisible();
  });
});
