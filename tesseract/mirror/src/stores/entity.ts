import { create } from 'zustand';
import type { EntityState } from '../lib/types';

interface EntityStoreState {
  state: EntityState;
  dreamingInFlight: boolean;
  accentHsl: string;
  setState: (s: EntityState) => void;
  setDreaming: (dreaming: boolean) => void;
  setAccentHsl: (hsl: string) => void;
  reset: () => void;
}

export const useEntityStore = create<EntityStoreState>((set, get) => ({
  state: 'idle',
  dreamingInFlight: false,
  accentHsl: '246 83% 68%',

  // Idempotent — skip the set() when the state isn't changing. Dispatch
  // fires setState('speaking') on every text_delta; without this guard
  // every token triggers a Zustand notification and re-renders every
  // subscriber (entity canvas, HUD, orb).
  setState: (s) => { if (get().state !== s) set({ state: s }); },
  setDreaming: (dreaming) => { if (get().dreamingInFlight !== dreaming) set({ dreamingInFlight: dreaming }); },
  setAccentHsl: (hsl) => { if (get().accentHsl !== hsl) set({ accentHsl: hsl }); },
  reset: () => set({ state: 'idle', dreamingInFlight: false }),
}));
