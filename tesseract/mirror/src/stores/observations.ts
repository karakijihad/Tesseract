import { create } from 'zustand';
import type { ObserverMode, ObserverStats } from '../lib/types';

export interface ObservationEntry {
  mode: ObserverMode;
  observation: string;
  timestamp: string;
  last_fire_ts: number | null;
}

const MAX_OBSERVATIONS = 10;

const EMPTY_STATS: ObserverStats = {
  fires_total: 0,
  tokens_used_total: 0,
  last_fired_at: null,
  circuit_breaker_state: 'green',
  pending_suggestion_count: 0,
};

interface ObservationsState {
  observations: ObservationEntry[];
  pending: boolean;
  stats: ObserverStats;
  fires_total: number;
  addObservation: (entry: Omit<ObservationEntry, 'last_fire_ts'> & { last_fire_ts?: number | null }) => void;
  setPending: (pending: boolean) => void;
  setStats: (stats: ObserverStats) => void;
  reset: () => void;
}

// Per-session observer activity — not persisted. The Observer panel
// reflects "what fired this session"; cleared on session_reset /
// session_loaded by `dispatch.ts`. Long-term forensics live in the
// JSONL log at `tesseract/logs/observations.jsonl`.
export const useObservationsStore = create<ObservationsState>()((set) => ({
  observations: [],
  pending: false,
  stats: EMPTY_STATS,
  fires_total: 0,

  addObservation: (entry) =>
    set((state) => {
      const stamped: ObservationEntry = {
        mode: entry.mode,
        observation: entry.observation,
        timestamp: entry.timestamp,
        last_fire_ts: entry.last_fire_ts ?? Date.now(),
      };
      // Keep the last (MAX_OBSERVATIONS - 1) entries, then append the
      // new one — total stays at MAX_OBSERVATIONS. Writing `-MAX - 1`
      // naively would leak an N+1 entry whenever MAX_OBSERVATIONS is
      // bumped, which is why this slice is `-(MAX - 1)`.
      return {
        observations: [
          ...state.observations.slice(-(MAX_OBSERVATIONS - 1)),
          stamped,
        ],
        fires_total: state.fires_total + 1,
      };
    }),

  setPending: (pending) => set({ pending }),

  setStats: (stats) => set({ stats }),

  reset: () =>
    set({
      observations: [],
      pending: false,
      stats: EMPTY_STATS,
      fires_total: 0,
    }),
}));
