// Orb hide/show — operator preference (on by default). Persisted to
// localStorage, operator-private per-machine like the cockpit layout and the
// captions toggle it mirrors. A single boolean. Written from two places: the
// HUD toggle and the TARS orb_visibility WS envelope (dispatch.ts).

import { create } from 'zustand';

const KEY = 'tesseract.cockpit.orb.visible';

function load(): boolean {
  try {
    const v = localStorage.getItem(KEY);
    return v === null ? true : v === '1'; // default on
  } catch {
    return true;
  }
}

function persist(visible: boolean): void {
  try {
    localStorage.setItem(KEY, visible ? '1' : '0');
  } catch {
    // private-mode / quota — best-effort.
  }
}

interface OrbVisibilityState {
  visible: boolean;
  toggle: () => void;
  /** Absolute setter — used by the TARS-driven WS path (orb_visibility
   *  envelope) so chat/voice can show/hide the orb. Persists the same as
   *  the HUD toggle: TARS acting on the orb is an operator-intent action. */
  setVisible: (visible: boolean) => void;
}

export const useOrbVisibilityStore = create<OrbVisibilityState>((set, get) => ({
  visible: load(),
  toggle: () => {
    const visible = !get().visible;
    persist(visible);
    set({ visible });
  },
  setVisible: (visible: boolean) => {
    if (visible === get().visible) return;
    persist(visible);
    set({ visible });
  },
}));
