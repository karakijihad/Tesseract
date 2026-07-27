// CV-1 — lanes store: attach reattach-detection, poll accumulation, send.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useLanesStore } from './lanes';

function jsonResponse(body: unknown, ok = true, status = 200) {
  return Promise.resolve({ ok, status, json: () => Promise.resolve(body) } as Response);
}

describe('useLanesStore', () => {
  beforeEach(() => {
    useLanesStore.setState({ byLane: {} });
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('attach seeds events + flags reattach when the snapshot replays history', async () => {
    vi.stubGlobal('fetch', () =>
      jsonResponse({
        recent_events: [{ kind: 'assistant_text', payload: { text: 'prior' } }],
        next_cursor: '42',
        status: { alive: true, busy: false, queue_depth: 0, last_activity_utc: 'x' },
      }),
    );
    await useLanesStore.getState().attach('lane-1');
    const st = useLanesStore.getState().byLane['lane-1'];
    expect(st.events).toHaveLength(1);
    expect(st.cursor).toBe('42');
    expect(st.reattachedAt).not.toBeNull(); // non-empty replay → reattach signal
  });

  it('attach on an empty lane does not flag reattach', async () => {
    vi.stubGlobal('fetch', () =>
      jsonResponse({ recent_events: [], next_cursor: '0', status: null }),
    );
    await useLanesStore.getState().attach('lane-2');
    expect(useLanesStore.getState().byLane['lane-2'].reattachedAt).toBeNull();
  });

  it('poll appends new events and advances the cursor', async () => {
    useLanesStore.setState({
      byLane: { 'lane-3': { events: [], cursor: '0', status: null, reattachedAt: null, offline: false, gone: false, goneStreak: 0 } },
    });
    vi.stubGlobal('fetch', (url: string) => {
      if (url.includes('/read')) {
        return jsonResponse({ events: [{ kind: 'assistant_text', payload: { text: 'hi' } }], next_cursor: '10' });
      }
      return jsonResponse({ alive: true, busy: true, queue_depth: 0, last_activity_utc: 'x' });
    });
    await useLanesStore.getState().poll('lane-3');
    const st = useLanesStore.getState().byLane['lane-3'];
    expect(st.events).toHaveLength(1);
    expect(st.cursor).toBe('10');
    expect(st.status?.busy).toBe(true);
  });

  it('send posts the message and reports ok', async () => {
    const spy = vi.fn(() => jsonResponse({ accepted: true, queue_depth: 0 }));
    vi.stubGlobal('fetch', spy);
    const ok = await useLanesStore.getState().send('lane-4', 'do it');
    expect(ok).toBe(true);
    expect(spy).toHaveBeenCalledWith(expect.stringContaining('/api/lanes/lane-4/send'), expect.objectContaining({ method: 'POST' }));
  });

  it('marks the lane offline on a 503', async () => {
    vi.stubGlobal('fetch', () => jsonResponse({}, false, 503));
    await useLanesStore.getState().attach('lane-5');
    expect(useLanesStore.getState().byLane['lane-5'].offline).toBe(true);
  });

  it('marks the lane gone after repeated lane-level (502) failures', async () => {
    vi.stubGlobal('fetch', () => jsonResponse({}, false, 502));
    await useLanesStore.getState().poll('lane-6');
    expect(useLanesStore.getState().byLane['lane-6'].gone).toBe(false); // streak 1
    await useLanesStore.getState().poll('lane-6');
    expect(useLanesStore.getState().byLane['lane-6'].gone).toBe(true); // streak 2 → gone
  });

  it('a 503 marks offline but never gone (controller down ≠ lane gone)', async () => {
    vi.stubGlobal('fetch', () => jsonResponse({}, false, 503));
    await useLanesStore.getState().poll('lane-7');
    await useLanesStore.getState().poll('lane-7');
    const st = useLanesStore.getState().byLane['lane-7'];
    expect(st.offline).toBe(true);
    expect(st.gone).toBe(false);
  });

  it('a transient 500 marks offline but never gone (only 502/404 dismiss a card)', async () => {
    vi.stubGlobal('fetch', () => jsonResponse({}, false, 500));
    await useLanesStore.getState().poll('lane-9');
    await useLanesStore.getState().poll('lane-9');
    const st = useLanesStore.getState().byLane['lane-9'];
    expect(st.offline).toBe(true);
    expect(st.gone).toBe(false);
  });

  it('attach then poll both 502 reaches the gone threshold', async () => {
    vi.stubGlobal('fetch', () => jsonResponse({}, false, 502));
    await useLanesStore.getState().attach('lane-10'); // streak 1
    expect(useLanesStore.getState().byLane['lane-10'].gone).toBe(false);
    await useLanesStore.getState().poll('lane-10'); // streak 2 → gone
    expect(useLanesStore.getState().byLane['lane-10'].gone).toBe(true);
  });

  it('a successful poll clears a gone streak', async () => {
    vi.stubGlobal('fetch', () => jsonResponse({}, false, 502));
    await useLanesStore.getState().poll('lane-8');
    await useLanesStore.getState().poll('lane-8');
    expect(useLanesStore.getState().byLane['lane-8'].gone).toBe(true);
    vi.stubGlobal('fetch', (url: string) =>
      url.includes('/read')
        ? jsonResponse({ events: [], next_cursor: '1' })
        : jsonResponse({ alive: true, busy: false, queue_depth: 0, last_activity_utc: 'x' }),
    );
    await useLanesStore.getState().poll('lane-8');
    const st = useLanesStore.getState().byLane['lane-8'];
    expect(st.gone).toBe(false);
    expect(st.goneStreak).toBe(0);
  });
});
