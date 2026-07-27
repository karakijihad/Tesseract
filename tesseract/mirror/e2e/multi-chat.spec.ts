import { test, expect, type Page } from '@playwright/test';

// multichat-redesign acceptance — the RENDERING layer of the dropdown chat
// manager (trigger, open-chat list, active highlight, per-row + aggregate
// approval badges, archived section, permanent delete, open-count). Driven
// through `__tesseractTestStores` against a mocked backend (no Python server,
// no LLM) — the existing e2e pattern. Backend WS round-trips are covered by
// tests/mirror (Python) and the dispatch vitest suites; this asserts what only
// a real DOM + CSS can.

const A = 'a'.repeat(32);
const B = 'b'.repeat(32);

// Archived-chats library returned for the archived section's GET /api/chats.
// Only this endpoint is mocked — every other boot fetch is left to fail
// naturally (connection refused to the absent backend), which the app handles
// gracefully and still mounts.
const ARCHIVED_CHATS = {
  chats: [
    { chat_id: 'e'.repeat(32), title: 'Archived Alpha', created_at: '2026-06-30T09:00:00', archived: true, message_count: 4 },
    { chat_id: 'f'.repeat(32), title: 'Still Open', created_at: '2026-06-30T10:00:00', archived: false, message_count: 1 },
  ],
};

async function boot(page: Page): Promise<void> {
  await page.route(/\/api\/chats(\?|$)/, async (route) => {
    await route.fulfill({ contentType: 'application/json', body: JSON.stringify(ARCHIVED_CHATS) });
  });
  await page.goto('/');
  // NOT networkidle — the app polls/retries on an interval (and the WS reconnect
  // loop never settles with no backend), so the network is never idle. The
  // cockpit mounts ChatView as a glass PANEL (viewRegistry), so open it via
  // panel.openPanel('chat') rather than ui.setView. Retry inside waitForFunction:
  // StrictMode's dev double-mount briefly deletes `__tesseractTestStores` (effect
  // cleanup) between mounts, so a wait-then-evaluate would race into that gap.
  await page.waitForFunction(() => {
    const s = (window as any).__tesseractTestStores;
    if (!s?.panel) return false;
    try { s.panel.getState().openPanel('chat'); return true; } catch { return false; }
  }, null, { timeout: 30_000 });
  await expect(page.locator('.chat-view')).toBeVisible({ timeout: 15_000 });
}

// Reset the conversation store to a known empty state, then seed `chats`.
async function seedChats(page: Page, ids: { id: string; title: string }[], activeId: string): Promise<void> {
  await page.evaluate(({ ids, activeId }) => {
    const conv = (window as any).__tesseractTestStores.conversation.getState();
    (window as any).__tesseractTestStores.conversation.setState({ chats: new Map(), orderedIds: [], activeChatId: null });
    const s = (window as any).__tesseractTestStores.conversation.getState();
    for (const { id, title } of ids) { s.initChat(id); s.setChatTitle(id, title); }
    (window as any).__tesseractTestStores.conversation.setState({ activeChatId: activeId });
    void conv;
  }, { ids, activeId });
}

const openPanel = (page: Page) => page.locator('.chat-mgr-trigger').click();

test.describe('multi-chat manager', () => {
  test.beforeEach(async ({ page }) => { await boot(page); });

  test('trigger shows the active chat and the panel lists every open chat', async ({ page }) => {
    await seedChats(page, [{ id: A, title: 'Chat A' }, { id: B, title: 'Chat B' }], A);
    await expect(page.locator('.chat-mgr-active-title')).toHaveText('Chat A');
    await openPanel(page);
    await expect(page.locator('.chat-mgr-row')).toHaveCount(2);
    await expect(page.locator('.chat-mgr-row.is-active .chat-mgr-row-title')).toHaveText('Chat A');
    // Move active to B in the store → highlight follows.
    await page.evaluate((b) => (window as any).__tesseractTestStores.conversation.getState().initChat(b), B);
    await expect(page.locator('.chat-mgr-row.is-active .chat-mgr-row-title')).toHaveText('Chat B');
  });

  test('aggregate approval dot marks the trigger when a background chat awaits a tool ASK', async ({ page }) => {
    await seedChats(page, [{ id: A, title: 'Chat A' }, { id: B, title: 'Chat B' }], A); // A active
    await page.evaluate((b) => {
      (window as any).__tesseractTestStores.conversation.getState().addApproval(b, {
        call_id: 'c1', name: 'web_search', input: {}, reason: '', received_at: 0, resolved: false,
      });
    }, B);
    await expect(page.locator('.chat-mgr-trigger .chat-tab-approval')).toHaveCount(1);
    // Inside the panel, the per-row badge is on B (the background chat), not active A.
    await openPanel(page);
    const badgedRow = page.locator('.chat-mgr-row', { has: page.locator('.chat-tab-approval') });
    await expect(badgedRow.locator('.chat-mgr-row-title')).toHaveText('Chat B');
  });

  test('archived section lists archived chats and exposes restore + permanent delete', async ({ page }) => {
    await seedChats(page, [{ id: A, title: 'Chat A' }], A);
    await openPanel(page);
    await page.locator('.chat-mgr-archived-header').click();
    await expect(page.locator('.chat-mgr-archived-row')).toHaveCount(1); // only the archived one
    await expect(page.locator('.chat-mgr-archived-title')).toHaveText('Archived Alpha');
    await expect(page.locator('.chat-mgr-restore')).toBeVisible();
    // Permanent delete is confirm-gated.
    await page.locator('[aria-label="Delete permanently"]').click();
    await expect(page.locator('.chat-mgr-confirm-yes')).toBeVisible();
  });

  test('open-count reflects the number of open chats', async ({ page }) => {
    const ids = Array.from({ length: 9 }, (_, i) => ({ id: String(i).repeat(32).slice(0, 32), title: `C${i}` }));
    await seedChats(page, ids, ids[0].id);
    await expect(page.locator('.chat-mgr-count')).toHaveText('9/10');
  });

  test('empty open-chat set shows a disabled trigger label and the panel new-chat affordance', async ({ page }) => {
    await page.evaluate(() => (window as any).__tesseractTestStores.conversation.setState({
      chats: new Map(), orderedIds: [], activeChatId: null,
    }));
    await expect(page.locator('.chat-mgr-active-title')).toHaveText('Chats');
    await openPanel(page);
    await expect(page.locator('.chat-mgr-new')).toContainText('New chat');
    await expect(page.locator('.chat-mgr-empty')).toContainText('No open chats');
  });
});
