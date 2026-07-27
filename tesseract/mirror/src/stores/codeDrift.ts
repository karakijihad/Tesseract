import { create } from 'zustand';

// History-bearing companion to the Autonomy header chip.
//
// Each `code_drift_detected` envelope pushes a `DriftEvent` onto `events`
// (newest first, cap MAX_EVENTS). The chip derives its visible state from
// the worst non-ignored event so multiple consecutive drifts don't spam
// the toast stack — the operator can open the chip menu to see the full
// history with timestamps + sample paths.
//
// `ignored` is a soft suppression that the operator toggles from the
// chip menu — it clears the chip's red/yellow accent but keeps history
// visible. A fresh drift envelope automatically resets `ignored` so a
// new event always re-asserts the status.

export type CodeDriftStatus = 'ok' | 'frontend_only' | 'restart_required';

export interface DriftEvent {
  id: string;
  classification: 'frontend_only' | 'restart_required';
  paths: string[];
  pathCount: number;
  headSha: string | null;
  detectedAt: string;
}

const MAX_EVENTS = 20;

interface CodeDriftStore {
  events: DriftEvent[];
  ignored: boolean;
  pushDrift: (payload: {
    classification: 'frontend_only' | 'restart_required';
    paths: string[];
    headSha: string | null;
    detectedAt: string;
  }) => void;
  ignore: () => void;
  reset: () => void;
  clearHistory: () => void;
}

let _eventSeq = 0;

export const useCodeDriftStore = create<CodeDriftStore>((set) => ({
  events: [],
  ignored: false,
  pushDrift: (payload) => set((s) => {
    _eventSeq += 1;
    const ev: DriftEvent = {
      id: `drift-${Date.now()}-${_eventSeq}`,
      classification: payload.classification,
      paths: payload.paths,
      pathCount: payload.paths.length,
      headSha: payload.headSha,
      detectedAt: payload.detectedAt,
    };
    const next = [ev, ...s.events].slice(0, MAX_EVENTS);
    return { events: next, ignored: false };
  }),
  ignore: () => set({ ignored: true }),
  reset: () => set({ events: [], ignored: false }),
  clearHistory: () => set({ events: [], ignored: false }),
}));

export interface DriftSelection {
  status: CodeDriftStatus;
  pathCount: number;
  headSha: string | null;
  hasRestartRequired: boolean;
}

export function deriveDriftSelection(events: DriftEvent[]): DriftSelection {
  if (events.length === 0) {
    return { status: 'ok', pathCount: 0, headSha: null, hasRestartRequired: false };
  }
  const hasRestart = events.some((e) => e.classification === 'restart_required');
  const status: CodeDriftStatus = hasRestart ? 'restart_required' : 'frontend_only';
  const latest = events[0];
  return {
    status,
    pathCount: latest.pathCount,
    headSha: latest.headSha,
    hasRestartRequired: hasRestart,
  };
}
