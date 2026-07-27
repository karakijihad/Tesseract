import { describe, it, expect, beforeEach, vi } from 'vitest';
import { useActivityStore, type ActivityRecord } from './activity';

function rec(id: string, state = 'running', kind = 'lane'): ActivityRecord {
  return {
    activity_id: id, kind, label: id, state, durability: 'persistent',
    provider: 'claude', parent_turn_id: null, parent_session_id: null,
    transcript_ref: null, started_at: '2026-06-29T00:00:00Z',
    updated_at: '2026-06-29T00:00:00Z',
  };
}

describe('useActivityStore', () => {
  beforeEach(() => useActivityStore.setState({ byId: {} }));

  function env(type: string, data: Record<string, unknown> | ActivityRecord, session_id = '') {
    return {
      type, category: 'activity' as const,
      session_id: session_id || ((data as ActivityRecord).activity_id as string) || '',
      timestamp: '2026-06-29T00:00:00Z', data,
    };
  }

  it('upsert adds then replaces by activity_id', () => {
    useActivityStore.getState().upsert(rec('lane:1', 'running'));
    useActivityStore.getState().upsert(rec('lane:1', 'idle'));
    expect(useActivityStore.getState().records()).toHaveLength(1);
    expect(useActivityStore.getState().byId['lane:1'].state).toBe('idle');
  });

  it('remove drops a record', () => {
    useActivityStore.getState().upsert(rec('lane:1'));
    useActivityStore.getState().remove('lane:1');
    expect(useActivityStore.getState().records()).toHaveLength(0);
  });

  it('runningCount counts spawning + running only', () => {
    useActivityStore.getState().upsert(rec('lane:1', 'running'));
    useActivityStore.getState().upsert(rec('lane:2', 'spawning'));
    useActivityStore.getState().upsert(rec('lane:3', 'idle'));
    expect(useActivityStore.getState().runningCount()).toBe(2);
  });

  it('hydrate replaces byId from GET /api/activity items', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true, json: async () => ({ items: [rec('lane:9')] }),
    }));
    await useActivityStore.getState().hydrate();
    expect(useActivityStore.getState().byId['lane:9']).toBeTruthy();
    vi.unstubAllGlobals();
  });

  it('applyEnvelope upserts on activity_registered', () => {
    useActivityStore.getState().applyEnvelope(env('activity_registered', rec('lane:1', 'running')) as never);
    expect(useActivityStore.getState().byId['lane:1'].state).toBe('running');
  });

  it('applyEnvelope upserts on activity_updated', () => {
    useActivityStore.getState().upsert(rec('lane:1', 'running'));
    useActivityStore.getState().applyEnvelope(env('activity_updated', rec('lane:1', 'idle')) as never);
    expect(useActivityStore.getState().byId['lane:1'].state).toBe('idle');
  });

  it('applyEnvelope removes on activity_removed (id from data)', () => {
    useActivityStore.getState().upsert(rec('lane:1'));
    useActivityStore.getState().applyEnvelope(env('activity_removed', rec('lane:1')) as never);
    expect(useActivityStore.getState().byId['lane:1']).toBeUndefined();
  });

  it('applyEnvelope removes on activity_removed using session_id fallback when data lacks an id', () => {
    useActivityStore.getState().upsert(rec('lane:1'));
    useActivityStore.getState().applyEnvelope(env('activity_removed', {}, 'lane:1') as never);
    expect(useActivityStore.getState().byId['lane:1']).toBeUndefined();
  });

  it('records() sorts running/spawning first, ahead of idle/done rows regardless of started_at', () => {
    useActivityStore.getState().upsert({ ...rec('lane:old', 'done'), started_at: '2026-06-01T00:00:00Z', updated_at: '2026-06-01T00:00:00Z' });
    useActivityStore.getState().upsert({ ...rec('lane:new', 'running'), started_at: '2026-06-29T00:00:00Z', updated_at: '2026-06-29T00:00:00Z' });
    const ids = useActivityStore.getState().records().map((r) => r.activity_id);
    expect(ids).toEqual(['lane:new', 'lane:old']);
  });

  it('records() orders newest updated_at first within the running bucket', () => {
    useActivityStore.getState().upsert({ ...rec('lane:a', 'running'), updated_at: '2026-06-29T00:00:00Z' });
    useActivityStore.getState().upsert({ ...rec('lane:b', 'spawning'), updated_at: '2026-06-30T00:00:00Z' });
    const ids = useActivityStore.getState().records().map((r) => r.activity_id);
    expect(ids).toEqual(['lane:b', 'lane:a']);
  });

  it('records() orders newest updated_at first within the non-running bucket', () => {
    useActivityStore.getState().upsert({ ...rec('lane:a', 'done'), updated_at: '2026-06-29T00:00:00Z' });
    useActivityStore.getState().upsert({ ...rec('lane:b', 'idle'), updated_at: '2026-06-30T00:00:00Z' });
    const ids = useActivityStore.getState().records().map((r) => r.activity_id);
    expect(ids).toEqual(['lane:b', 'lane:a']);
  });
});
