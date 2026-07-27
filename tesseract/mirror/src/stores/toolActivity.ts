import { create } from 'zustand';

interface ToolActivityState {
  lastTool: string | null;
  firedAt: number;
  setLastTool: (name: string) => void;
  clear: () => void;
}

export const useToolActivityStore = create<ToolActivityState>((set) => ({
  lastTool: null,
  firedAt: 0,
  setLastTool: (name) => set({ lastTool: name, firedAt: Date.now() }),
  clear: () => set({ lastTool: null, firedAt: 0 }),
}));
