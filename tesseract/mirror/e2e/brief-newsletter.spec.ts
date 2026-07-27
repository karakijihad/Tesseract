import { test, expect } from '@playwright/test';
import type { Page } from '@playwright/test';

// MO-9-14 — the daily-brief newsletter is a workspace event card, not
// a dedicated tab. These specs mock the workspace inbox to return a
// single `daily_brief` event and verify (a) the card renders the
// three pillars with per-card metadata, (b) clicking 👍 fires the
// feedback POST with the right shape, (c) the per-card guard prevents
// double-firing.

const _NOW = '2026-05-14T08:00:00+00:00';

const _BRIEF_EVENT = {
  event_id: 'evt_brief_test',
  ts: _NOW,
  kind: 'daily_brief',
  source: 'daily_brief',
  title: 'Daily brief — 2026-05-14',
  summary: 'Two missions closed.',
  priority: 4,
  status: 'pending',
  decided_at: null,
  decided_reason: null,
  author_id: 'system',
  author_display: 'TARS',
  delivered_to_tars: false,
  comments: [],
  payload: {
    kind: 'daily_brief',
    date: '2026-05-14',
    sections: {
      yesterday_in_tesseract: 'Two missions closed and one rolled forward.',
      yesterday_with_you: 'DONE — Mission alpha. BLOCKED — Mission beta on lease.',
      what_i_learned: 'Belief about routing solidified through three reflections.',
      vault: ['New — Phase 17 portability notes.', 'Updated — soul-growth rubric.'],
      world: {
        tech: [
          {
            title: 'Local-first revival lands',
            summary: 'A wide-ranging look at the local-first software movement and what it means for ordinary users this year.',
            source: 'Ars Technica',
            url: 'https://example.org/local-first',
            published_at: '2026-05-13',
          },
        ],
        science: [
          {
            title: 'Climate model refresh',
            summary: 'Carbon Brief covers the new climate-model refresh and what it shifts in projections.',
            source: 'carbonbrief.org',
            url: 'https://example.org/climate',
            published_at: '2026-05-13',
          },
        ],
        politics: [],
      },
    },
    cost_cap_reached: false,
  },
};

async function _mockInbox(page: Page): Promise<void> {
  await page.route(/\/api\/workspace\/inbox(\?.*)?$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ events: [_BRIEF_EVENT] }),
    });
  });
  await page.route(/\/api\/workspace\/seen$/, async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({}),
      });
      return;
    }
    await route.fulfill({ status: 204, body: '' });
  });
}

test('brief newsletter card renders the three pillars with metadata', async ({ page }) => {
  await _mockInbox(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Workspace', exact: true }).click();
  // Brief card appears in the inbox; expand it.
  const briefCard = page.locator('article.workspace-event', { hasText: 'Daily brief — 2026-05-14' });
  await expect(briefCard).toBeVisible();
  await briefCard.getByRole('button').first().click();

  // The card body lists the three pillar headings.
  await expect(page.getByRole('heading', { name: 'Tech', level: 4 })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Science', level: 4 })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Politics', level: 4 })).toBeVisible();
  // Per-card title + source.
  await expect(page.getByText('Local-first revival lands')).toBeVisible();
  await expect(page.getByText('Ars Technica')).toBeVisible();
  // Empty pillar shows the no-fresh-items placeholder.
  await expect(page.locator('.brief-pillar', { hasText: 'Politics' })
    .getByText('No fresh items today.')).toBeVisible();
});

test('thumbs-up fires POST /api/brief/feedback with the right shape', async ({ page }) => {
  await _mockInbox(page);
  let captured: Record<string, unknown> | null = null;
  await page.route(/\/api\/brief\/feedback$/, async (route) => {
    captured = JSON.parse(route.request().postData() || '{}') as Record<string, unknown>;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        date: captured.date,
        pillar: captured.pillar,
        topic: captured.url,
        signal: 'INTERESTED',
        affinity: { 'https://example.org/local-first': 1.0 },
      }),
    });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Workspace', exact: true }).click();
  const briefCard = page.locator('article.workspace-event', { hasText: 'Daily brief — 2026-05-14' });
  await briefCard.getByRole('button').first().click();
  // Tech card 👍 → first .brief-card-action under the Tech pillar block.
  const techCard = page.locator('.brief-pillar', { hasText: 'Tech' })
    .locator('.brief-card').first();
  await techCard.getByRole('button', { name: 'More like this' }).click();
  await expect(page.getByText('noted').first()).toBeVisible();
  expect(captured).not.toBeNull();
  expect(captured?.date).toBe('2026-05-14');
  expect(captured?.pillar).toBe('tech');
  expect(captured?.url).toBe('https://example.org/local-first');
  expect(captured?.signal).toBe('interested');
});

test('per-card buttons disable after first click to prevent double-fire', async ({ page }) => {
  await _mockInbox(page);
  let hits = 0;
  await page.route(/\/api\/brief\/feedback$/, async (route) => {
    hits += 1;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        date: '2026-05-14',
        pillar: 'tech',
        topic: 'x',
        signal: 'INTERESTED',
        affinity: {},
      }),
    });
  });
  await page.goto('/');
  await page.getByRole('button', { name: 'Workspace', exact: true }).click();
  const briefCard = page.locator('article.workspace-event', { hasText: 'Daily brief — 2026-05-14' });
  await briefCard.getByRole('button').first().click();
  const techCard = page.locator('.brief-pillar', { hasText: 'Tech' })
    .locator('.brief-card').first();
  const btn = techCard.getByRole('button', { name: 'More like this' });
  await btn.click();
  await expect(page.getByText('noted').first()).toBeVisible();
  // All four reaction buttons disable once one signal lands.
  for (const label of ['More like this', 'Less like this', 'Dig deeper on this topic', 'Open a comment thread']) {
    await expect(techCard.getByRole('button', { name: label })).toBeDisabled();
  }
  expect(hits).toBe(1);
});
