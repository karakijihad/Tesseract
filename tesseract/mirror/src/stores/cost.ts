/**
 * Cost store — per-role spend + dual-ceiling budget state from the backend
 * `CostLedger`. One `cost_delta` envelope per chat/observer turn updates
 * `perRole[role]` and the shared `globalState`.
 *
 * Persist only `perRole` + `globalState` so reopening the Mirror mid-day
 * shows the current meter immediately; the backend re-broadcasts on the
 * next turn anyway.
 */

import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import type {
  CostBudgetStateData,
  CostDeltaData,
  CostOverageAskData,
  CostStateData,
} from '../lib/types';

export interface CostPerRole {
  role: string;
  model: string;
  role_total_usd: number;
  role_cap_usd: number | null;
  last_cost_usd: number;
  last_at: string;
}

interface CostStore {
  perRole: Record<string, CostPerRole>;
  globalState: CostBudgetStateData | null;
  voiceProviders: CostStateData['voice_providers'] | null;
  // Cost UX overhaul: scope_keys the operator approved-to-continue
  // today. HUD chips for these scopes render red when spent>cap (the
  // overage display) instead of blocked.
  overageUnlocked: string[];
  warned: string[];
  // In-flight 100% overage confirmation requests. Each card stays
  // until the operator clicks Yes/No (backend then resolves the future
  // and the card is removed). Not persisted — on reload the backend
  // re-asks if it still needs approval.
  pendingOverageAsks: CostOverageAskData[];
  pushOverageAsk: (data: CostOverageAskData) => void;
  resolveOverageAsk: (call_id: string) => void;
  applyDelta: (env: { timestamp: string; data: CostDeltaData }) => void;
  applySnapshot: (env: { timestamp: string; data: CostStateData }) => void;
  unlockOverage: (scope_key: string) => void;
  reset: () => void;
}

export const useCostStore = create<CostStore>()(
  persist(
    (set) => ({
      perRole: {},
      globalState: null,
      voiceProviders: null,
      overageUnlocked: [],
      warned: [],
      pendingOverageAsks: [],
      pushOverageAsk: (data) => {
        set((prev) =>
          prev.pendingOverageAsks.some((a) => a.call_id === data.call_id)
            ? prev
            : { pendingOverageAsks: [...prev.pendingOverageAsks, data] },
        );
      },
      resolveOverageAsk: (call_id) => {
        set((prev) => ({
          pendingOverageAsks: prev.pendingOverageAsks.filter((a) => a.call_id !== call_id),
        }));
      },
      applyDelta: (env) => {
        const d = env.data;
        set((prev) => ({
          perRole: {
            ...prev.perRole,
            [d.role]: {
              role: d.role,
              model: d.model,
              role_total_usd: d.role_total_usd,
              role_cap_usd: d.state.role_cap_usd,
              last_cost_usd: d.cost_usd,
              last_at: env.timestamp,
            },
          },
          globalState: d.state,
        }));
      },
      applySnapshot: (env) => {
        // Catch-up replaces the full role map so stale localStorage entries
        // (e.g. yesterday's totals after midnight rollover) are dropped, not
        // merged. The model/last_at fields aren't carried by the snapshot —
        // we keep prior values where present so the chip tooltip doesn't
        // briefly read empty until the next billed turn fills them in.
        const d = env.data;
        set((prev) => {
          const nextPerRole: Record<string, CostPerRole> = {};
          for (const [role, entry] of Object.entries(d.roles)) {
            const prior = prev.perRole[role];
            nextPerRole[role] = {
              role,
              model: prior?.model ?? entry.last_model,
              role_total_usd: entry.role_total_usd,
              role_cap_usd: entry.role_cap_usd,
              last_cost_usd: prior?.last_cost_usd ?? 0,
              last_at: prior?.last_at ?? env.timestamp,
            };
          }
          return {
            perRole: nextPerRole,
            globalState: {
              spent_usd: d.global.spent_usd,
              warning_usd: d.global.warning_usd,
              cap_usd: d.global.cap_usd,
              role_spent_usd: 0,
              role_cap_usd: null,
              warning: d.global.warning,
              blocked: d.global.blocked,
            },
            voiceProviders: d.voice_providers,
            overageUnlocked: d.overage_unlocked ?? [],
            warned: d.warned ?? [],
          };
        });
      },
      unlockOverage: (scope_key: string) => {
        set((prev) =>
          prev.overageUnlocked.includes(scope_key)
            ? prev
            : { overageUnlocked: [...prev.overageUnlocked, scope_key] },
        );
      },
      reset: () => set({
        perRole: {},
        globalState: null,
        voiceProviders: null,
        overageUnlocked: [],
        warned: [],
      }),
    }),
    {
      name: 'tesseract-mirror-cost',
      storage: createJSONStorage(() => localStorage),
      // Bump version to invalidate stale localStorage when chips lock to
      // pre-restart values. The backend re-broadcasts a fresh cost_state on
      // every WS connect, so a clean hydration is harmless.
      version: 2,
      migrate: (_persisted, _version) => ({
        perRole: {},
        globalState: null,
        voiceProviders: null,
        overageUnlocked: [],
        warned: [],
        pendingOverageAsks: [],
      }),
      partialize: (state) => ({
        perRole: state.perRole,
        globalState: state.globalState,
        voiceProviders: state.voiceProviders,
        // overageUnlocked / warned are intentionally NOT persisted. The
        // backend ledger clears them on midnight rollover and on process
        // restart (in-memory only) — see ledger.py docstring "No on-disk
        // persistence — restarting Mirror re-asks". Persisting them on
        // the frontend would survive a server restart and silently keep
        // chips red against a fresh backend state until the next billed
        // turn refreshed the snapshot.
      }),
    },
  ),
);
