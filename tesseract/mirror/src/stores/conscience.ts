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

/** `YYYY-MM-DD`, inclusive both ends. `null` on either means "let the backend
 *  choose" — which it does by opening on the last 30 days. */
export interface DriftRange {
  from: string | null;
  to: string | null;
}

/** One tool's usage inside one window. `sessions` is the rank, never `calls`:
 *  a loop calling one tool four hundred times is one session's worth of
 *  evidence. `calls: 0` is a real row — the registry is joined in, so a tool
 *  nobody has touched appears as a zero rather than going missing. */
export interface ToolUsageRow {
  tool: string;
  sessions: number;
  calls: number;
}

export interface ToolUsageWindow {
  days: number;
  tools: ToolUsageRow[];
}

interface ConscienceState {
  report: DriftReport | null;
  history: DriftReport[];
  /** Every date with a report on disk, oldest first. The picker is bounded by
   *  this rather than by a calendar: only these days can return anything. */
  available: string[];
  /** What the backend actually resolved the window to, which is what the
   *  inputs show — asking for an open-ended range and displaying blanks would
   *  leave the operator unable to tell what they are looking at. */
  range: DriftRange;
  loading: boolean;
  error: string | null;
  fetchDrift: (range?: DriftRange) => Promise<void>;

  /** Usage per window, in the order the backend returned them. */
  usage: ToolUsageWindow[];
  /** False when the backend had no registry to join — the panel says so
   *  rather than showing a roster it cannot vouch for. */
  usageRoster: boolean;
  usageTotal: number;
  usageLoading: boolean;
  usageError: string | null;
  fetchToolUsage: () => Promise<void>;
}

export const useConscienceStore = create<ConscienceState>((set) => ({
  report: null,
  history: [],
  available: [],
  range: { from: null, to: null },
  loading: false,
  error: null,

  usage: [],
  usageRoster: false,
  usageTotal: 0,
  usageLoading: false,
  usageError: null,

  fetchToolUsage: async () => {
    set({ usageLoading: true, usageError: null });
    try {
      const res = await fetch(`${BACKEND_BASE}/api/conscience/tool-usage`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as {
        windows: ToolUsageWindow[];
        roster: boolean;
        total_tools: number;
      };
      set({
        usage: data.windows ?? [],
        usageRoster: Boolean(data.roster),
        usageTotal: data.total_tools ?? 0,
        usageLoading: false,
      });
    } catch (err) {
      set({
        usageError: err instanceof Error ? err.message : String(err),
        usageLoading: false,
      });
    }
  },

  fetchDrift: async (range) => {
    set({ loading: true, error: null });
    try {
      const params = new URLSearchParams();
      if (range?.from) params.set('from', range.from);
      if (range?.to) params.set('to', range.to);
      const query = params.toString();
      const res = await fetch(
        `${BACKEND_BASE}/api/conscience/drift${query ? `?${query}` : ''}`,
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as {
        report: DriftReport | null;
        history: DriftReport[];
        available?: string[];
        from?: string;
        to?: string;
      };
      set({
        report: data.report ?? null,
        history: data.history ?? [],
        available: data.available ?? [],
        range: { from: data.from ?? null, to: data.to ?? null },
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
