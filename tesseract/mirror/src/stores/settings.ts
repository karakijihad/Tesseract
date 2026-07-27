import { create } from 'zustand';

const LS_KEY = 'settings.collapsedSections';

interface SettingsStore {
  collapsedSections: Record<string, boolean>;
  toggleCollapsed: (section: string) => void;
  setCollapsed: (section: string, collapsed: boolean) => void;
}

function loadCollapsed(): Record<string, boolean> {
  if (typeof window === 'undefined') return {};
  try {
    const raw = window.localStorage.getItem(LS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      const out: Record<string, boolean> = {};
      for (const [k, v] of Object.entries(parsed)) {
        if (typeof v === 'boolean') out[k] = v;
      }
      return out;
    }
  } catch {
    // corrupted localStorage value — reset on next write
  }
  return {};
}

function persistCollapsed(state: Record<string, boolean>): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(LS_KEY, JSON.stringify(state));
  } catch {
    // storage full / disabled — state still lives in memory for this session
  }
}

export const useSettingsStore = create<SettingsStore>((set) => ({
  collapsedSections: loadCollapsed(),
  toggleCollapsed: (section) =>
    set((state) => {
      const next = { ...state.collapsedSections, [section]: !state.collapsedSections[section] };
      persistCollapsed(next);
      return { collapsedSections: next };
    }),
  setCollapsed: (section, collapsed) =>
    set((state) => {
      const next = { ...state.collapsedSections, [section]: collapsed };
      persistCollapsed(next);
      return { collapsedSections: next };
    }),
}));
