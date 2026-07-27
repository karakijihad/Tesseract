import { create } from 'zustand';
import { fetchBreakers as apiFetchBreakers } from '../lib/api';

export interface Breaker {
  name: string;
  state: 'closed' | 'open';
  failureCount: number;
  lastFailure: string | null;
  lastReset: string | null;
}

interface HealthState {
  breakers: Breaker[];
  setBreakers: (breakers: Breaker[]) => void;
  upsertBreaker: (breaker: Breaker) => void;
  fetchBreakers: () => Promise<void>;
  reset: () => void;
}

export const useHealthStore = create<HealthState>((set) => ({
  breakers: [],
  setBreakers: (breakers) => set({ breakers }),
  upsertBreaker: (breaker) =>
    set((state) => {
      const idx = state.breakers.findIndex((b) => b.name === breaker.name);
      if (idx === -1) return { breakers: [...state.breakers, breaker] };
      const next = state.breakers.slice();
      next[idx] = breaker;
      return { breakers: next };
    }),
  async fetchBreakers() {
    try {
      const res = await apiFetchBreakers();
      const mapped: Breaker[] = res.breakers.map((b) => ({
        name: b.name,
        state: b.state,
        failureCount: b.failure_count,
        lastFailure: b.last_failure,
        lastReset: b.last_reset,
      }));
      set({ breakers: mapped });
    } catch {
      /* backend unavailable */
    }
  },
  reset: () => set({ breakers: [] }),
}));
