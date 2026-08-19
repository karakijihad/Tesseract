import { create } from 'zustand';

interface ToolActivityState {
  lastTool: string | null;
  firedAt: number;
  /** When the newest tool RESULT landed. Lights the rail's `result` stage. */
  resultAt: number;
  /** Fires per tool name this session. The Kernel rail counts by group. */
  counts: Record<string, number>;
  /**
   * Call ids already counted. One delegate invocation emits BOTH
   * `stream_tool_call_end` and `cli_start`, and counting each would show a
   * single delegation as two fires on the agents group.
   */
  countedCalls: string[];
  setLastTool: (name: string, callId?: string) => void;
  markResult: () => void;
  clear: () => void;
}

export const useToolActivityStore = create<ToolActivityState>((set) => ({
  lastTool: null,
  firedAt: 0,
  resultAt: 0,
  counts: {},
  countedCalls: [],
  setLastTool: (name, callId) =>
    set((s) => {
      const counted = callId !== undefined && s.countedCalls.includes(callId);
      if (counted) return { lastTool: name, firedAt: Date.now() };
      return {
        lastTool: name,
        firedAt: Date.now(),
        counts: { ...s.counts, [name]: (s.counts[name] ?? 0) + 1 },
        countedCalls:
          callId === undefined ? s.countedCalls : [...s.countedCalls, callId],
      };
    }),
  markResult: () => set({ resultAt: Date.now() }),
  clear: () =>
    set({ lastTool: null, firedAt: 0, resultAt: 0, counts: {}, countedCalls: [] }),
}));
