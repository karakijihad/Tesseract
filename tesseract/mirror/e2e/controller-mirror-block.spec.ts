import { test, expect } from '@playwright/test';

// X-2 (Docs/Plan/tars-cockpit/phase-X-2-controller-daemon-default.md) —
// the controller card must surface independent of the tool-pill expansion
// gate (closes Codex audit-2026-06-01 H2), and the completion card must
// render the new `transcript_path` returned by
// `GET /api/controller_sessions/{id}` after the live WS drops.
//
// This spec replaces the deprecated *-screenshot.spec.ts pattern: the
// assertions are semantic (DOM presence + text content) rather than
// pixel-image. Playwright captures a failure-only screenshot to
// test-results/ if any assertion below fails (see `playwright.config.ts`).

const FAKE_SESSION_ID = '2026-06-02-x2deadbe';
const FAKE_WS_PATH = `/ws/controller/${FAKE_SESSION_ID}`;
const FAKE_TRANSCRIPT_PATH =
  '/home/op/.tesseract/tars_controller/transcripts/2026-06-02-x2deadbe.jsonl';

const STATUS_BODY_WITH_PATH = {
  session_id: FAKE_SESSION_ID,
  status: 'closed',
  mode: 'autonomy',
  origin: 'mirror',
  title: 'X-2 e2e',
  last_active_at: '2026-06-02T13:00:00Z',
  transcript_path: FAKE_TRANSCRIPT_PATH,
};

test.describe('X-2 — ControllerMirrorBlock surfaces by default + completion card carries transcript_path', () => {
  test.beforeEach(async ({ page }) => {
    // Only intercept the status route — the rest of /api/** falls through
    // to the (absent) backend at localhost:8000 and fails with ERR_CONNECTION_REFUSED.
    // The Mirror tolerates that during boot and still exposes the dev-only
    // ``__tesseractTestStores`` global. Mocking every endpoint with `{}`
    // breaks several stores whose schemas the parsers strictly require,
    // which would prevent the global from ever appearing.
    await page.route(`**/api/controller_sessions/${FAKE_SESSION_ID}`, async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify(STATUS_BODY_WITH_PATH),
      });
    });

    await page.goto('/', { waitUntil: 'domcontentloaded' });
    // App boots its dev-only test-store global inside a useEffect; wait
    // on that signal rather than networkidle (the chat WS reconnect loop
    // keeps networkidle from ever settling without a real backend).
    await page.waitForFunction(
      () => Boolean((window as unknown as { __tesseractTestStores?: unknown }).__tesseractTestStores),
      { timeout: 10_000 },
    );
    await page.evaluate(() => {
      (window as any).__tesseractTestStores.ui.getState().setView('chat');
    });
    await expect(page.locator('.chat-view')).toBeVisible();
  });

  test('controller card mounts even when the tool pill stays collapsed', async ({ page }) => {
    await page.evaluate(
      ({ sessionId, wsPath }) => {
        (window as any).__tesseractTestStores.conversation
          .getState()
          .loadHistory([
            {
              id: 'assistant-x2',
              role: 'assistant',
              content: 'Launching a controller session.',
              timestamp: Date.now(),
              status: 'complete',
              toolCalls: [
                {
                  call_id: 'call-x2-1',
                  name: 'start_controller_session',
                  input: { task: 'do a thing' },
                },
              ],
              toolResults: [
                {
                  call_id: 'call-x2-1',
                  output: 'session minted',
                  is_error: false,
                  metadata: {
                    kind: 'child_transcript_ref',
                    session_id: sessionId,
                    ws_path: wsPath,
                  },
                },
              ],
            },
          ]);
      },
      { sessionId: FAKE_SESSION_ID, wsPath: FAKE_WS_PATH },
    );

    const toolPill = page.locator('.tool-call-pill').first();
    await expect(toolPill).toBeVisible();
    // Pre-X-2, the controller card was inside the expand-on-click gate.
    // X-2 lift: the controller card must render OUTSIDE the gate. The
    // tool-pill-body (which IS the expand-gated region) should not be
    // present until the operator clicks the pill header.
    await expect(toolPill.locator('.tool-pill-body')).toHaveCount(0);

    // ControllerMirrorBlock + handoff card visible by default.
    const controllerCard = page.locator('.controller-mirror');
    await expect(controllerCard).toBeVisible();
    await expect(
      controllerCard.locator('.controller-mirror__handoff-label'),
    ).toHaveText(/launched terminal session/i);
    await expect(
      controllerCard.locator('.controller-mirror__session-id'),
    ).toHaveText(FAKE_SESSION_ID);
    await expect(
      controllerCard.locator('.controller-mirror__hint'),
    ).toHaveText(`tars --session ${FAKE_SESSION_ID}`);
  });

  test('completion card renders transcript_path returned by the status route', async ({ page }) => {
    await page.evaluate(
      ({ sessionId, wsPath }) => {
        (window as any).__tesseractTestStores.conversation
          .getState()
          .loadHistory([
            {
              id: 'assistant-x2-2',
              role: 'assistant',
              content: 'Launching.',
              timestamp: Date.now(),
              status: 'complete',
              toolCalls: [
                {
                  call_id: 'call-x2-2',
                  name: 'start_controller_session',
                  input: { task: 'a' },
                },
              ],
              toolResults: [
                {
                  call_id: 'call-x2-2',
                  output: 'session minted',
                  is_error: false,
                  metadata: {
                    kind: 'child_transcript_ref',
                    session_id: sessionId,
                    ws_path: wsPath,
                  },
                },
              ],
            },
          ]);
      },
      { sessionId: FAKE_SESSION_ID, wsPath: FAKE_WS_PATH },
    );

    // The block's controller WS targets ws://localhost:8000/ws/controller/...,
    // which fails ERR_CONNECTION_REFUSED with no backend running. That
    // fires onclose → the block fetches /api/controller_sessions/{id} →
    // the completion card renders with the mocked transcript_path.
    const statusCard = page.locator('.controller-mirror__status');
    await expect(statusCard).toBeVisible({ timeout: 10_000 });
    await expect(statusCard).toContainText(/finished/i);

    const pathLine = statusCard.locator('.controller-mirror__status-path');
    await expect(pathLine).toBeVisible();
    await expect(pathLine).toContainText(FAKE_TRANSCRIPT_PATH);
  });

  test('completion card omits the path line when transcript_path is null', async ({ page }) => {
    // Override the status route for this scenario only — drop the field.
    await page.unroute(`**/api/controller_sessions/${FAKE_SESSION_ID}`);
    await page.route(`**/api/controller_sessions/${FAKE_SESSION_ID}`, async (route) => {
      await route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({ ...STATUS_BODY_WITH_PATH, transcript_path: null }),
      });
    });

    await page.evaluate(
      ({ sessionId, wsPath }) => {
        (window as any).__tesseractTestStores.conversation
          .getState()
          .loadHistory([
            {
              id: 'assistant-x2-3',
              role: 'assistant',
              content: 'Launching.',
              timestamp: Date.now(),
              status: 'complete',
              toolCalls: [
                {
                  call_id: 'call-x2-3',
                  name: 'start_controller_session',
                  input: { task: 'a' },
                },
              ],
              toolResults: [
                {
                  call_id: 'call-x2-3',
                  output: 'session minted',
                  is_error: false,
                  metadata: {
                    kind: 'child_transcript_ref',
                    session_id: sessionId,
                    ws_path: wsPath,
                  },
                },
              ],
            },
          ]);
      },
      { sessionId: FAKE_SESSION_ID, wsPath: FAKE_WS_PATH },
    );

    const statusCard = page.locator('.controller-mirror__status');
    await expect(statusCard).toBeVisible({ timeout: 10_000 });
    await expect(
      statusCard.locator('.controller-mirror__status-path'),
    ).toHaveCount(0);
  });
});
