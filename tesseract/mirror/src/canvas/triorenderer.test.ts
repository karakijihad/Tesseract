// restoreLanes — re-surface last session's surviving lanes as cards on reload,
// sourced from the AS-1 activity registry (live lanes only). Idempotent against
// cards already on the canvas; does NOT ensure/spawn new lanes.

import { afterEach, describe, expect, it, vi } from 'vitest';

import { restoreLanes } from './triorenderer';

interface Created {
  type: string;
  props: Record<string, unknown>;
}

function mockFetch(activity: unknown, surfaces: unknown[]) {
  const created: Created[] = [];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith('/api/activity')) {
      return { ok: true, json: async () => activity } as Response;
    }
    if (url.includes('/api/surfaces/')) {
      if (init?.method === 'POST') {
        const body = JSON.parse(String(init.body)) as Created;
        created.push(body);
        return { ok: true, json: async () => ({}) } as Response;
      }
      return { ok: true, json: async () => ({ surfaces }) } as Response;
    }
    return { ok: false, json: async () => ({}) } as Response;
  });
  vi.stubGlobal('fetch', fetchMock);
  return { created };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('restoreLanes', () => {
  it('creates a lane card for each live lane in the activity registry', async () => {
    const { created } = mockFetch(
      {
        items: [
          { activity_id: 'lane:lane-claude-1', kind: 'lane', label: 'coder/claude', provider: 'claude', state: 'idle' },
          { activity_id: 'lane:lane-codex-2', kind: 'lane', label: 'auditor/codex', provider: 'codex', state: 'running' },
          { activity_id: 'session:s1', kind: 'controller_session', label: 'chat', state: 'idle' },
          { activity_id: 'delegate:d1', kind: 'delegate', label: 'delegate_claude', state: 'running' },
        ],
      },
      [],
    );
    const restored = await restoreLanes('tars');
    expect(restored).toEqual(['lane-claude-1', 'lane-codex-2']); // lanes only, not session/delegate
    expect(created).toHaveLength(2);
    expect(created.map((c) => c.type)).toEqual(['lane', 'lane']);
    expect(created[0].props).toMatchObject({ lane_id: 'lane-claude-1', name: 'coder/claude', kind: 'claude' });
    expect(created[1].props).toMatchObject({ lane_id: 'lane-codex-2', name: 'auditor/codex', kind: 'codex' });
  });

  it('skips a lane whose card is already on the canvas (idempotent)', async () => {
    const { created } = mockFetch(
      { items: [{ activity_id: 'lane:lane-claude-1', kind: 'lane', label: 'coder/claude', provider: 'claude', state: 'idle' }] },
      [{ type: 'lane', props: { lane_id: 'lane-claude-1' } }],
    );
    const restored = await restoreLanes('tars');
    expect(restored).toEqual([]);
    expect(created).toHaveLength(0);
  });

  it('no live lanes → no cards created', async () => {
    const { created } = mockFetch({ items: [{ activity_id: 'session:s1', kind: 'controller_session', label: 'chat', state: 'idle' }] }, []);
    const restored = await restoreLanes('tars');
    expect(restored).toEqual([]);
    expect(created).toHaveLength(0);
  });

  it('does NOT restore a card for a closed/dead lane (avoids the 502 storm)', async () => {
    const { created } = mockFetch(
      {
        items: [
          { activity_id: 'lane:lane-live-1', kind: 'lane', label: 'coder/claude', provider: 'claude', state: 'idle' },
          { activity_id: 'lane:lane-dead-2', kind: 'lane', label: 'auditor/codex', provider: 'codex', state: 'closed' },
          { activity_id: 'lane:lane-dead-3', kind: 'lane', label: 'x', provider: 'claude', state: 'failed' },
        ],
      },
      [],
    );
    const restored = await restoreLanes('tars');
    expect(restored).toEqual(['lane-live-1']); // only the live lane; closed/failed skipped
    expect(created).toHaveLength(1);
  });
});
