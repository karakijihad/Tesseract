// Y-2 — Surface Protocol store. Holds TARS-spawned canvas surfaces per
// view, hydrated from REST on canvas mount and kept live via the `canvas`
// WS category (re-keyed from the `surface` background-bus channel). Operator
// interactions update optimistically + POST back through `emitSurfaceEvent`.

import { create } from "zustand";

import { BACKEND_BASE } from "../lib/endpoints";
import { nextZ, recordSurfaceZ } from "../cockpit/zStack";
import type { Envelope } from "../lib/types";
import {
  hydrateMap,
  reduceSurfaceEvent,
  type HighlightPulse,
  type SurfaceMap,
} from "../canvas/protocol/dispatch";
import { emitSurfaceEvent } from "../canvas/protocol/events";
import type {
  SurfaceDescriptor,
  SurfacePosition,
  SurfaceSize,
} from "../canvas/protocol/types";

interface SurfacesState {
  byView: Record<string, SurfaceMap>;
  // Latest highlight pulse per surface (transient; renderer fades it).
  highlights: Record<string, HighlightPulse>;
  // AS-2 — transient client-side stacking order (NOT persisted, NOT sent to
  // the backend). Drawn from the shared zStack so surface cards interleave
  // with SC-2 view panels on a single last-focused-wins counter.
  liveZ: Record<string, number>;
  // Client-side minimized set (NOT persisted, NOT sent to the backend).
  // Collapsed cards stay mounted so their lane streams keep running.
  minimized: Record<string, boolean>;
  // Client-side maximized set (NOT persisted, NOT sent to the backend). A
  // maximized card fills the surface-layer overlay; restore returns it to the
  // stored descriptor geometry (which maximize never mutates).
  maximized: Record<string, boolean>;
  raiseSurface: (id: string) => void;
  toggleMinimize: (view: string, id: string) => void;
  toggleMaximize: (view: string, id: string) => void;
  hydrate: (view: string) => Promise<void>;
  applyEnvelope: (env: Envelope) => void;
  surfacesFor: (view: string) => SurfaceDescriptor[];
  moveSurface: (view: string, id: string, position: SurfacePosition) => void;
  resizeSurface: (view: string, id: string, size: SurfaceSize) => void;
  // Live (no-emit, no-persist) updates during a drag/resize so dependent
  // visuals (e.g. trio wires) follow in real time; the move/resize commit on
  // pointer-up emits + persists.
  dragSurface: (view: string, id: string, position: SurfacePosition) => void;
  dragResize: (view: string, id: string, size: SurfaceSize) => void;
  closeSurface: (view: string, id: string) => void;
  renameSurface: (view: string, id: string, title: string) => void;
}

function sortedByZ(map: SurfaceMap): SurfaceDescriptor[] {
  return Object.values(map).sort((a, b) => (a.z ?? 0) - (b.z ?? 0));
}

export const useSurfacesStore = create<SurfacesState>((set, get) => ({
  byView: {},
  highlights: {},
  liveZ: {},
  minimized: {},
  maximized: {},
  raiseSurface: (id) => {
    const z = nextZ();
    recordSurfaceZ(z);
    set((s) => ({ liveZ: { ...s.liveZ, [id]: z } }));
  },

  // Collapse the card off-stage (kept mounted so the lane stream survives).
  // Restoring raises to the front, matching the panelStore.toggleMinimize idiom.
  toggleMinimize: (_view, id) => {
    set((s) => {
      const wasMinimized = s.minimized[id] ?? false;
      const next = { ...s.minimized, [id]: !wasMinimized };
      if (wasMinimized) {
        // Restore — raise to front.
        const z = nextZ();
        recordSurfaceZ(z);
        return { minimized: next, liveZ: { ...s.liveZ, [id]: z } };
      }
      return { minimized: next };
    });
  },

  // Fill the overlay (geometry computed at render from measured bounds);
  // maximizing raises to front. Restore just flips the flag — descriptor
  // geometry is untouched, so the card returns to its stored position/size.
  toggleMaximize: (_view, id) => {
    set((s) => {
      const wasMaximized = s.maximized[id] ?? false;
      const next = { ...s.maximized, [id]: !wasMaximized };
      if (!wasMaximized) {
        const z = nextZ();
        recordSurfaceZ(z);
        return { maximized: next, liveZ: { ...s.liveZ, [id]: z } };
      }
      return { maximized: next };
    });
  },

  hydrate: async (view) => {
    try {
      const resp = await fetch(
        `${BACKEND_BASE}/api/surfaces/${encodeURIComponent(view)}`,
      );
      if (!resp.ok) return;
      const body = (await resp.json()) as { surfaces?: unknown[] };
      const map = hydrateMap(Array.isArray(body.surfaces) ? body.surfaces : []);
      set((s) => ({ byView: { ...s.byView, [view]: map } }));
    } catch (err) {
      console.error(`surfaces: hydrate ${view} threw`, err);
    }
  },

  applyEnvelope: (env) => {
    // session_id carries the view name (orchestrator/surfaces/events.py).
    const view = env.session_id;
    if (!view) return;
    const current = get().byView[view] ?? {};
    const result = reduceSurfaceEvent(current, env);
    set((s) => {
      const next: Partial<SurfacesState> = {
        byView: { ...s.byView, [view]: result.map },
      };
      if (result.highlight) {
        next.highlights = {
          ...s.highlights,
          [result.highlight.surface_id]: result.highlight,
        };
      }
      return next;
    });
  },

  surfacesFor: (view) => sortedByZ(get().byView[view] ?? {}),

  moveSurface: (view, id, position) => {
    set((s) => {
      const map = s.byView[view];
      const desc = map?.[id];
      if (!desc) return s;
      return {
        byView: {
          ...s.byView,
          [view]: { ...map, [id]: { ...desc, position } },
        },
      };
    });
    void emitSurfaceEvent(view, id, "moved", { position });
  },

  resizeSurface: (view, id, size) => {
    set((s) => {
      const map = s.byView[view];
      const desc = map?.[id];
      if (!desc) return s;
      return {
        byView: { ...s.byView, [view]: { ...map, [id]: { ...desc, size } } },
      };
    });
    void emitSurfaceEvent(view, id, "resized", { size });
  },

  dragSurface: (view, id, position) => {
    set((s) => {
      const map = s.byView[view];
      const desc = map?.[id];
      if (!desc) return s;
      return {
        byView: {
          ...s.byView,
          [view]: { ...map, [id]: { ...desc, position } },
        },
      };
    });
  },

  dragResize: (view, id, size) => {
    set((s) => {
      const map = s.byView[view];
      const desc = map?.[id];
      if (!desc) return s;
      return {
        byView: { ...s.byView, [view]: { ...map, [id]: { ...desc, size } } },
      };
    });
  },

  closeSurface: (view, id) => {
    set((s) => {
      const map = s.byView[view];
      if (!map || !(id in map)) return s;
      const next = { ...map };
      delete next[id];
      return { byView: { ...s.byView, [view]: next } };
    });
    void emitSurfaceEvent(view, id, "closed", {});
  },

  renameSurface: (view, id, title) => {
    set((s) => {
      const map = s.byView[view];
      const desc = map?.[id];
      if (!desc) return s;
      return {
        byView: { ...s.byView, [view]: { ...map, [id]: { ...desc, title } } },
      };
    });
    void fetch(
      `${BACKEND_BASE}/api/surfaces/${encodeURIComponent(view)}/${encodeURIComponent(id)}/update`,
      {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ title }),
      },
    ).catch(() => undefined);
  },
}));
