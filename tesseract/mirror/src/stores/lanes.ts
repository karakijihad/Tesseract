// CV-1 — live controller-lane state for canvas lane cards.
//
// Each lane card (LaneRenderer) drives one lane_id: `attach` seeds the
// snapshot + cursor (the brain-restart recovery primitive), `poll` pulls
// new events since the cursor, `send` posts a follow-up. The lane authority
// is the controller daemon; Mirror reaches it via the REST bridge
// (routes/lanes.py). Events accumulate per lane, deduped by cursor.

import { create } from 'zustand';

import { BACKEND_BASE } from '../lib/endpoints';

export interface LaneEvent {
  kind: string;
  payload: Record<string, unknown>;
  at_utc?: string;
  cursor?: string;
}

export interface LaneStatus {
  alive: boolean;
  busy: boolean;
  queue_depth: number;
  last_activity_utc: string;
  current_turn_id?: string | null;
}

interface LaneState {
  events: LaneEvent[];
  cursor: string | null;
  status: LaneStatus | null;
  reattachedAt: string | null; // set when attach replays a non-empty snapshot
  offline: boolean;
  gone: boolean; // lane no longer exists in the controller (repeated lane-level errors)
  goneStreak: number; // consecutive lane-level failures; internal
}

// A "lane not found" response (502 lane-level error / 404) this many times in a
// row → the lane is gone; the card stops polling (else a stale card that
// outlived its lane — e.g. across a restart — storms the backend forever).
// Only 502/404 count as gone: 503 (controller offline) and other transient 5xx
// (500/504) are NOT definitive lane-gone signals, so they never dismiss a card.
const GONE_THRESHOLD = 2;

function markFailure(prev: LaneState, status: number): LaneState {
  const laneGone = status === 502 || status === 404;
  if (!laneGone) return { ...prev, offline: true, goneStreak: 0, gone: false };
  const goneStreak = prev.goneStreak + 1;
  return { ...prev, offline: false, goneStreak, gone: goneStreak >= GONE_THRESHOLD };
}

interface LanesStore {
  byLane: Record<string, LaneState>;
  attach: (laneId: string) => Promise<void>;
  poll: (laneId: string) => Promise<void>;
  send: (laneId: string, message: string) => Promise<boolean>;
  clear: (laneId: string) => void;
  close: (laneId: string) => Promise<boolean>;
  laneState: (laneId: string) => LaneState | undefined;
}

const EMPTY: LaneState = {
  events: [],
  cursor: null,
  status: null,
  reattachedAt: null,
  offline: false,
  gone: false,
  goneStreak: 0,
};

function laneUrl(laneId: string, suffix = ''): string {
  return `${BACKEND_BASE}/api/lanes/${encodeURIComponent(laneId)}${suffix}`;
}

export const useLanesStore = create<LanesStore>((set, get) => ({
  byLane: {},

  attach: async (laneId) => {
    try {
      const resp = await fetch(laneUrl(laneId, '/attach'), { method: 'POST' });
      if (!resp.ok) {
        set((s) => ({ byLane: { ...s.byLane, [laneId]: markFailure(s.byLane[laneId] ?? EMPTY, resp.status) } }));
        return;
      }
      const snap = (await resp.json()) as {
        recent_events?: LaneEvent[];
        next_cursor?: string;
        status?: LaneStatus;
      };
      const events = Array.isArray(snap.recent_events) ? snap.recent_events : [];
      set((s) => {
        const prev = s.byLane[laneId];
        // Re-attach signal: fire only when this is a genuine reconnect to a
        // lane that survived without us — either the store had no prior
        // knowledge of it this session (fresh page load → the lane outlived
        // a brain restart) or its cursor advanced while we were detached.
        // A benign same-session remount (store still holds the lane, cursor
        // unchanged) must NOT re-raise the banner.
        const wasKnown = prev != null && prev.cursor != null;
        const cursorChanged = wasKnown && (snap.next_cursor ?? null) !== prev.cursor;
        const reattached =
          events.length > 0 && (!wasKnown || cursorChanged)
            ? new Date().toISOString()
            : (prev?.reattachedAt ?? null);
        return {
          byLane: {
            ...s.byLane,
            [laneId]: {
              events,
              cursor: snap.next_cursor ?? null,
              status: snap.status ?? null,
              reattachedAt: reattached,
              offline: false,
              gone: false,
              goneStreak: 0,
            },
          },
        };
      });
    } catch {
      set((s) => ({ byLane: { ...s.byLane, [laneId]: { ...(s.byLane[laneId] ?? EMPTY), offline: true } } }));
    }
  },

  poll: async (laneId) => {
    const cur = get().byLane[laneId]?.cursor ?? '';
    try {
      const [readResp, statusResp] = await Promise.all([
        fetch(laneUrl(laneId, `/read?cursor=${encodeURIComponent(cur)}`)),
        fetch(laneUrl(laneId, '/status')),
      ]);
      if (!readResp.ok) {
        set((s) => ({ byLane: { ...s.byLane, [laneId]: markFailure(s.byLane[laneId] ?? EMPTY, readResp.status) } }));
        return;
      }
      const read = (await readResp.json()) as { events?: LaneEvent[]; next_cursor?: string };
      const status = statusResp.ok ? ((await statusResp.json()) as LaneStatus) : null;
      set((s) => {
        const prev = s.byLane[laneId] ?? EMPTY;
        const fresh = Array.isArray(read.events) ? read.events : [];
        return {
          byLane: {
            ...s.byLane,
            [laneId]: {
              ...prev,
              events: fresh.length > 0 ? [...prev.events, ...fresh] : prev.events,
              cursor: read.next_cursor ?? prev.cursor,
              status: status ?? prev.status,
              offline: false,
              gone: false,
              goneStreak: 0,
            },
          },
        };
      });
    } catch {
      set((s) => ({ byLane: { ...s.byLane, [laneId]: { ...(s.byLane[laneId] ?? EMPTY), offline: true } } }));
    }
  },

  send: async (laneId, message) => {
    try {
      const resp = await fetch(laneUrl(laneId, '/send'), {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ message }),
      });
      return resp.ok;
    } catch {
      return false;
    }
  },

  // Clear the displayed transcript (view-only — new events still append;
  // a re-attach replays full history from disk).
  clear: (laneId) => {
    set((s) => {
      const prev = s.byLane[laneId];
      if (!prev) return s;
      return { byLane: { ...s.byLane, [laneId]: { ...prev, events: [], reattachedAt: null } } };
    });
  },

  // Terminate the lane (operator delete). Distinct from dismissing the card.
  close: async (laneId) => {
    try {
      const resp = await fetch(laneUrl(laneId, '/close'), {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ reason: 'operator_close' }),
      });
      return resp.ok;
    } catch {
      return false;
    }
  },

  laneState: (laneId) => get().byLane[laneId],
}));
