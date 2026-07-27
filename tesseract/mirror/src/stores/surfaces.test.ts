// Y-2 — surfaces store: WS envelope application + view scoping.

import { beforeEach, describe, expect, it } from 'vitest';

import type { Envelope } from '../lib/types';
import { useSurfacesStore } from './surfaces';
import type { SurfaceDescriptor } from '../canvas/protocol/types';

function desc(id: string, view = 'tars'): SurfaceDescriptor {
  return {
    schema_version: 1,
    id,
    type: 'folder',
    view,
    position: { x: 0, y: 0 },
    size: { w: 400, h: 300 },
    z: 1,
    created_at_utc: '2026-06-03T00:00:00Z',
    updated_at_utc: '2026-06-03T00:00:00Z',
  };
}

function env(type: string, view: string, data: Record<string, unknown>): Envelope {
  return { type, category: 'canvas', session_id: view, timestamp: '2026-06-03T00:00:01Z', data };
}

describe('useSurfacesStore', () => {
  beforeEach(() => {
    useSurfacesStore.setState({ byView: {}, highlights: {} });
  });

  it('applyEnvelope adds a created surface under its view', () => {
    useSurfacesStore.getState().applyEnvelope(env('surface_created', 'tars', desc('a') as never));
    expect(useSurfacesStore.getState().surfacesFor('tars').map((d) => d.id)).toEqual(['a']);
    // Other views untouched.
    expect(useSurfacesStore.getState().surfacesFor('pulse')).toEqual([]);
  });

  it('applyEnvelope ignores an envelope with no view', () => {
    useSurfacesStore.getState().applyEnvelope(env('surface_created', '', desc('a') as never));
    expect(useSurfacesStore.getState().byView).toEqual({});
  });

  it('surfacesFor returns surfaces ordered by z', () => {
    useSurfacesStore.getState().applyEnvelope(env('surface_created', 'tars', { ...desc('a'), z: 3 } as never));
    useSurfacesStore.getState().applyEnvelope(env('surface_created', 'tars', { ...desc('b'), z: 1 } as never));
    expect(useSurfacesStore.getState().surfacesFor('tars').map((d) => d.id)).toEqual(['b', 'a']);
  });

  it('applyEnvelope records a highlight pulse', () => {
    useSurfacesStore.getState().applyEnvelope(env('surface_created', 'tars', desc('a') as never));
    useSurfacesStore.getState().applyEnvelope(env('surface_highlighted', 'tars', { surface_id: 'a', persistent: false }));
    expect(useSurfacesStore.getState().highlights.a?.surface_id).toBe('a');
  });
});

describe('toggleMinimize', () => {
  beforeEach(() => {
    useSurfacesStore.setState({
      byView: { tars: { 'lane-1': desc('lane-1') } },
      highlights: {},
      liveZ: {},
      minimized: {},
    });
  });

  it('toggleMinimize flips minimized true then false, keeping the surface in byView', () => {
    const s = useSurfacesStore.getState();
    // Surface exists before minimize.
    expect(s.surfacesFor('tars').map((d) => d.id)).toContain('lane-1');

    s.toggleMinimize('tars', 'lane-1');
    expect(useSurfacesStore.getState().minimized['lane-1']).toBe(true);
    // Surface still mounted (still in byView).
    expect(useSurfacesStore.getState().surfacesFor('tars').map((d) => d.id)).toContain('lane-1');

    useSurfacesStore.getState().toggleMinimize('tars', 'lane-1');
    expect(useSurfacesStore.getState().minimized['lane-1']).toBe(false);
    // Restore raises to front — liveZ entry set.
    expect(useSurfacesStore.getState().liveZ['lane-1']).toBeGreaterThan(0);
  });
});
