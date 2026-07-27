import { create } from 'zustand';
import { BACKEND_BASE } from '../lib/endpoints';

export type SignalStatus = 'ok' | 'warn' | 'bad';

export interface SignalResult {
  name: string;
  value: number;
  status: SignalStatus;
  warn: number;
  bad: number;
  detail: string;
}

export interface DriftReport {
  timestamp: string;
  window_hours: number;
  signals: SignalResult[];
  summary: { ok: number; warn: number; bad: number };
}

interface ConscienceState {
  report: DriftReport | null;
  history: DriftReport[];
  loading: boolean;
  error: string | null;
  fetchDrift: () => Promise<void>;
}

export const useConscienceStore = create<ConscienceState>((set) => ({
  report: null,
  history: [],
  loading: false,
  error: null,

  fetchDrift: async () => {
    set({ loading: true, error: null });
    try {
      const res = await fetch(`${BACKEND_BASE}/api/conscience/drift`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as { report: DriftReport | null; history: DriftReport[] };
      set({
        report: data.report ?? null,
        history: data.history ?? [],
        loading: false,
      });
    } catch (err) {
      set({
        error: err instanceof Error ? err.message : String(err),
        loading: false,
      });
    }
  },
}));
