import { create } from 'zustand';

import { fetchTools, postToolPermission } from '../lib/api';
import type { ToolEntry } from '../lib/types';

export type Posture = 'auto' | 'ask' | 'deny';

interface PrevPosture {
  permission: string;
  default_posture: string;
}

interface ToolsStore {
  tools: ToolEntry[] | null;
  mode: string | null;
  loading: boolean;
  error: string | null;
  /** Bumped every time the user requests a refresh. ToolsSection effects can
   *  watch this to trigger refetch without owning their own request state. */
  refreshTick: number;
  load: (force?: boolean) => Promise<void>;
  invalidate: () => void;
  setPostureOptimistic: (name: string, posture: Posture) => PrevPosture | null;
  rollback: (name: string, prev: PrevPosture) => void;
}

export const useToolsStore = create<ToolsStore>((set, get) => ({
  tools: null,
  mode: null,
  loading: false,
  error: null,
  refreshTick: 0,
  load: async (force = false) => {
    const { loading, tools } = get();
    if (loading) return;
    if (!force && tools !== null) return;
    set({ loading: true, error: null });
    try {
      const res = await fetchTools();
      set({ tools: res.tools, mode: res.mode, loading: false });
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : 'tools fetch failed',
        loading: false,
      });
    }
  },
  invalidate: () => {
    set((s) => ({ tools: null, refreshTick: s.refreshTick + 1 }));
  },
  setPostureOptimistic: (name, posture) => {
    const current = get().tools;
    if (!current) return null;
    const entry = current.find((t) => t.name === name);
    if (!entry) return null;
    const prev: PrevPosture = {
      permission: entry.permission,
      default_posture: entry.default_posture,
    };
    // The /api/settings/tool-permission route writes only `tools.<name>` (the
    // default). When the current mode has an override active for this tool,
    // the *effective* posture stays the override value — don't overwrite it
    // with the new default. A post-save refetch reconciles authoritative state.
    set({
      tools: current.map((t) => {
        if (t.name !== name) return t;
        return {
          ...t,
          default_posture: posture,
          permission: t.mode_override ? t.permission : posture,
        };
      }),
    });
    return prev;
  },
  rollback: (name, prev) => {
    const current = get().tools;
    if (!current) return;
    set({
      tools: current.map((t) =>
        t.name === name
          ? { ...t, permission: prev.permission, default_posture: prev.default_posture }
          : t,
      ),
    });
  },
}));

export async function applyToolPermission(name: string, posture: Posture): Promise<void> {
  const store = useToolsStore.getState();
  const prev = store.setPostureOptimistic(name, posture);
  try {
    await postToolPermission(name, posture);
  } catch (err) {
    if (prev !== null) store.rollback(name, prev);
    throw err;
  }
}
