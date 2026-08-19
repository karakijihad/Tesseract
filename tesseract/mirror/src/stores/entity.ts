import { create } from 'zustand';
import type { EntityState } from '../lib/types';

import { resolveHazes } from '../lib/entity/haze';

/** The orb is WebGL, so no custom property reaches it. This store is the one
 *  channel from the brand file to the canvas: `appearance.ts::apply` reads the
 *  tokens off `:root` and pushes them here, and `EntityController` forwards
 *  them to the particle system and the haze. `accentHsl` took this path first;
 *  the ground, the dreaming colour and the per-state hazes now take it too. */
interface EntityStoreState {
  state: EntityState;
  dreamingInFlight: boolean;
  accentHsl: string;
  /** `--orb-ground` — the colour the haze fades out to. */
  orbGround: string;
  /** `--orb-dreaming` — the one colour that belongs to a mood, not a state. */
  orbDreaming: string;
  /** Resolved per state: custom, then derived from a shifted accent, then the
   *  shipped default. `haze.ts::resolveHazes` owns that precedence. */
  hazes: Record<EntityState, string>;
  setState: (s: EntityState) => void;
  setDreaming: (dreaming: boolean) => void;
  setAccentHsl: (hsl: string) => void;
  setOrbPalette: (ground: string, dreaming: string) => void;
  setHazes: (hazes: Record<EntityState, string>) => void;
  reset: () => void;
}

export const useEntityStore = create<EntityStoreState>((set, get) => ({
  state: 'idle',
  dreamingInFlight: false,
  accentHsl: '246 83% 68%',
  orbGround: '#050508',
  orbDreaming: '#6b21a8',
  hazes: resolveHazes(0, false, {}),

  // Idempotent — skip the set() when the state isn't changing. Dispatch
  // fires setState('speaking') on every text_delta; without this guard
  // every token triggers a Zustand notification and re-renders every
  // subscriber (entity canvas, HUD, orb).
  setState: (s) => { if (get().state !== s) set({ state: s }); },
  setDreaming: (dreaming) => { if (get().dreamingInFlight !== dreaming) set({ dreamingInFlight: dreaming }); },
  setAccentHsl: (hsl) => { if (get().accentHsl !== hsl) set({ accentHsl: hsl }); },
  setOrbPalette: (ground, dreaming) => {
    if (get().orbGround === ground && get().orbDreaming === dreaming) return;
    set({ orbGround: ground, orbDreaming: dreaming });
  },
  // Compared by value: `apply` runs on every appearance write, and a fresh
  // object each time would notify the controller on a font change.
  setHazes: (hazes) => {
    const cur = get().hazes;
    for (const k of Object.keys(hazes) as EntityState[]) {
      if (cur[k] !== hazes[k]) { set({ hazes }); return; }
    }
  },
  reset: () => set({ state: 'idle', dreamingInFlight: false }),
}));
