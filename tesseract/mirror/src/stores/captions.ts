// Ambient orb captions — operator preference (on by default; dismissable).
// Persisted to localStorage, operator-private per-machine like the cockpit
// layout. A single boolean; no backend involvement.

import { create } from 'zustand';

const KEY = 'tesseract.cockpit.captions.enabled';

function load(): boolean {
  try {
    const v = localStorage.getItem(KEY);
    return v === null ? true : v === '1'; // default on
  } catch {
    return true;
  }
}

function persist(enabled: boolean): void {
  try {
    localStorage.setItem(KEY, enabled ? '1' : '0');
  } catch {
    // private-mode / quota — best-effort.
  }
}

interface CaptionsState {
  enabled: boolean;
  toggle: () => void;
  setEnabled: (enabled: boolean) => void;
}

export const useCaptionsStore = create<CaptionsState>((set, get) => ({
  enabled: load(),
  toggle: () => {
    const enabled = !get().enabled;
    persist(enabled);
    set({ enabled });
  },
  setEnabled: (enabled) => {
    persist(enabled);
    set({ enabled });
  },
}));
