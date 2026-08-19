import { create } from 'zustand';

const LS_KEY = 'settings.activeSection';

interface SettingsStore {
  /** Which section the rail has open. One at a time by construction — the
   *  pane shows a single body, so there is no set of open sections to track. */
  activeSection: string | null;
  setActiveSection: (section: string) => void;
}

function loadActive(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    const raw = window.localStorage.getItem(LS_KEY);
    return typeof raw === 'string' && raw ? raw : null;
  } catch {
    // corrupted / disabled localStorage — fall back to the rail's default
    return null;
  }
}

export const useSettingsStore = create<SettingsStore>((set) => ({
  activeSection: loadActive(),
  setActiveSection: (section) => {
    try {
      window.localStorage.setItem(LS_KEY, section);
    } catch {
      // storage full / disabled — the rail still switches, it just won't persist
    }
    set({ activeSection: section });
  },
}));
