// Y-2 — Surface Protocol verb-dispatcher reducer tests.

import { describe, expect, it } from 'vitest';

import type { Envelope } from '../../lib/types';
import { hydrateMap, reduceSurfaceEvent, type SurfaceMap } from './dispatch';
import type { SurfaceDescriptor } from './types';

function desc(over: Partial<SurfaceDescriptor> = {}): SurfaceDescriptor {
  return {
    schema_version: 1,
    id: 'folder-tars-1',
    type: 'folder',
    view: 'tars',
    position: { x: 10, y: 20 },
    size: { w: 600, h: 400 },
    z: 1,
    created_at_utc: '2026-06-03T00:00:00Z',
    updated_at_utc: '2026-06-03T00:00:00Z',
    ...over,
  };
}

function env(type: string, data: Record<string, unknown>): Envelope {
  return { type, category: 'canvas', session_id: 'tars', timestamp: '2026-06-03T00:00:01Z', data };
}

describe('reduceSurfaceEvent', () => {
  it('surface_created adds the descriptor', () => {
    const r = reduceSurfaceEvent({}, env('surface_created', desc() as never));
    expect(Object.keys(r.map)).toEqual(['folder-tars-1']);
  });

  it('surface_updated replaces the descriptor', () => {
    const base: SurfaceMap = { 'folder-tars-1': desc() };
    const r = reduceSurfaceEvent(base, env('surface_updated', desc({ title: 'X' }) as never));
    expect(r.map['folder-tars-1'].title).toBe('X');
  });

  it('rejects a v2 descriptor (forward-incompatible)', () => {
    const r = reduceSurfaceEvent({}, env('surface_created', desc({ schema_version: 2 }) as never));
    expect(Object.keys(r.map)).toHaveLength(0);
  });

  it('surface_focused bumps z', () => {
    const base: SurfaceMap = { a: desc({ id: 'a', z: 1 }) };
    const r = reduceSurfaceEvent(base, env('surface_focused', { surface_id: 'a', z: 9 }));
    expect(r.map.a.z).toBe(9);
  });

  it('surface_closed removes the surface', () => {
    const base: SurfaceMap = { a: desc({ id: 'a' }) };
    const r = reduceSurfaceEvent(base, env('surface_closed', { surface_id: 'a' }));
    expect(r.map.a).toBeUndefined();
  });

  it('surface_locked toggles lock', () => {
    const base: SurfaceMap = { a: desc({ id: 'a', locked: false }) };
    const r = reduceSurfaceEvent(base, env('surface_locked', { surface_id: 'a', locked: true }));
    expect(r.map.a.locked).toBe(true);
  });

  it('surface_bound attaches the session', () => {
    const base: SurfaceMap = { a: desc({ id: 'a' }) };
    const r = reduceSurfaceEvent(
      base,
      env('surface_bound', { surface_id: 'a', session_kind: 'lane', session_id: 'L1' }),
    );
    expect(r.map.a.bound_session).toEqual({ kind: 'lane', id: 'L1' });
  });

  it('surface_highlighted returns a pulse without mutating the map', () => {
    const base: SurfaceMap = { a: desc({ id: 'a' }) };
    const r = reduceSurfaceEvent(base, env('surface_highlighted', { surface_id: 'a', persistent: true }));
    expect(r.map).toBe(base);
    expect(r.highlight).toEqual({ surface_id: 'a', persistent: true, at: '2026-06-03T00:00:01Z' });
  });

  it('unknown event type is a no-op', () => {
    const base: SurfaceMap = { a: desc({ id: 'a' }) };
    const r = reduceSurfaceEvent(base, env('surface_teleported', { surface_id: 'a' }));
    expect(r.map).toBe(base);
  });
});

describe('hydrateMap', () => {
  it('keeps supported descriptors, drops malformed ones', () => {
    const map = hydrateMap([desc({ id: 'good' }), { junk: true }, desc({ id: 'v2', schema_version: 2 })]);
    expect(Object.keys(map)).toEqual(['good']);
  });
});
