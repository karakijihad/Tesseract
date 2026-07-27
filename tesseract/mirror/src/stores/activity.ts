// AS-2 — the frontend reflection of the AS-1 Unified Activity Registry.
// Hydrates once from GET /api/activity, then applies live deltas re-keyed
// from the `activity` WS channel. Backend is the source of truth; this
// store only mirrors — no orchestration, no persistence.

import { create } from "zustand";

import { BACKEND_BASE } from "../lib/endpoints";
import type { Envelope } from "../lib/types";

export interface ActivityRecord {
  activity_id: string;
  kind: string; // delegate | lane | controller_session (+ routine in AS-3)
  label: string;
  state: string; // spawning|running|idle|done|failed|cancelled|closed
  durability: string;
  provider: string | null;
  parent_turn_id: string | null;
  parent_session_id: string | null;
  transcript_ref: string | null;
  result?: string | null; // terminal outcome summary — the error detail for a `failed` chip
  started_at: string;
  updated_at: string;
}

// input_required (trio W4) = running-but-parked on an operator question —
// still an active unit for sort/badge purposes. N3: the single exported
// running-state classifier — ActivityMap imports this instead of duplicating
// the set (a drifted copy would misclassify a row).
export const RUNNING_STATUSES = new Set([
  "spawning",
  "running",
  "input_required",
]);
export const isRunningStatus = (state: string): boolean =>
  RUNNING_STATUSES.has(state);

interface ActivityState {
  byId: Record<string, ActivityRecord>;
  hydrate: () => Promise<void>;
  applyEnvelope: (env: Envelope) => void;
  upsert: (r: ActivityRecord) => void;
  remove: (id: string) => void;
  clear: () => void;
  records: () => ActivityRecord[];
  runningCount: () => number;
}

export const useActivityStore = create<ActivityState>((set, get) => ({
  byId: {},

  hydrate: async () => {
    try {
      const resp = await fetch(`${BACKEND_BASE}/api/activity`);
      if (!resp.ok) return;
      const body = (await resp.json()) as { items?: ActivityRecord[] };
      const next: Record<string, ActivityRecord> = {};
      for (const r of body.items ?? []) next[r.activity_id] = r;
      set({ byId: next });
    } catch (err) {
      console.error("activity: hydrate threw", err);
    }
  },

  applyEnvelope: (env) => {
    const data = env.data as unknown as ActivityRecord;
    if (env.type === "activity_removed") {
      get().remove(data?.activity_id ?? env.session_id);
      return;
    }
    if (data?.activity_id) get().upsert(data);
  },

  upsert: (r) => set((s) => ({ byId: { ...s.byId, [r.activity_id]: r } })),

  remove: (id) =>
    set((s) => {
      if (!(id in s.byId)) return s;
      const next = { ...s.byId };
      delete next[id];
      return { byId: next };
    }),

  clear: () => set({ byId: {} }),

  records: () =>
    Object.values(get().byId).sort((a, b) => {
      const ra = isRunningStatus(a.state) ? 0 : 1;
      const rb = isRunningStatus(b.state) ? 0 : 1;
      if (ra !== rb) return ra - rb;
      return a.updated_at < b.updated_at
        ? 1
        : a.updated_at > b.updated_at
          ? -1
          : 0;
    }),

  runningCount: () =>
    Object.values(get().byId).filter((r) => isRunningStatus(r.state)).length,
}));
