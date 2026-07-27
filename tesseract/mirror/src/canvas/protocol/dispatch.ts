// Y-2 — Surface Protocol verb dispatcher. Pure reducer: takes the current
// per-view surface map + an inbound WS event and returns the next map. The
// store (`stores/surfaces.ts`) owns the map and the side effects; this
// module is the protocol logic so it's unit-testable in isolation.

import type { Envelope } from '../../lib/types';
import {
  isSupportedDescriptor,
  type SurfaceDescriptor,
} from './types';

export type SurfaceMap = Record<string, SurfaceDescriptor>;

// Transient highlight state lives alongside the map but outside the
// descriptor (highlight is published, not persisted — see store.py).
export interface HighlightPulse {
  surface_id: string;
  persistent: boolean;
  at: string;
}

export interface DispatchResult {
  map: SurfaceMap;
  highlight?: HighlightPulse;
}

function withUpdate(
  map: SurfaceMap,
  id: string,
  patch: Partial<SurfaceDescriptor>,
): SurfaceMap {
  const current = map[id];
  if (!current) return map;
  return { ...map, [id]: { ...current, ...patch } };
}

// Apply a single WS surface event to the map. Unknown/malformed events are
// no-ops (return the same map) so a bad descriptor never crashes the canvas.
export function reduceSurfaceEvent(map: SurfaceMap, env: Envelope): DispatchResult {
  const data = env.data as Record<string, unknown>;
  switch (env.type) {
    case 'surface_created':
    case 'surface_updated': {
      if (!isSupportedDescriptor(data)) return { map };
      return { map: { ...map, [data.id]: data } };
    }
    case 'surface_focused': {
      const id = String(data.surface_id ?? '');
      const z = typeof data.z === 'number' ? data.z : undefined;
      return { map: z === undefined ? map : withUpdate(map, id, { z }) };
    }
    case 'surface_locked': {
      const id = String(data.surface_id ?? '');
      return { map: withUpdate(map, id, { locked: Boolean(data.locked) }) };
    }
    case 'surface_bound': {
      const id = String(data.surface_id ?? '');
      const kind = data.session_kind;
      const sid = data.session_id;
      if (typeof kind !== 'string' || typeof sid !== 'string') return { map };
      return { map: withUpdate(map, id, { bound_session: { kind, id: sid } }) };
    }
    case 'surface_closed': {
      const id = String(data.surface_id ?? '');
      if (!(id in map)) return { map };
      const next = { ...map };
      delete next[id];
      return { map: next };
    }
    case 'surface_highlighted': {
      const id = String(data.surface_id ?? '');
      if (!(id in map)) return { map };
      return {
        map,
        highlight: {
          surface_id: id,
          persistent: Boolean(data.persistent),
          at: env.timestamp,
        },
      };
    }
    default:
      return { map };
  }
}

// Build the initial map from a REST hydrate (list of descriptors).
export function hydrateMap(surfaces: unknown[]): SurfaceMap {
  const map: SurfaceMap = {};
  for (const raw of surfaces) {
    if (isSupportedDescriptor(raw)) map[raw.id] = raw;
  }
  return map;
}
