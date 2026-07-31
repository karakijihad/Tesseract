import { create } from "zustand";

// Sectioned dock (2026-07-31, operator-approved mockup v6) — shared state for
// the bottom HUD: which section stack is open (one at a time) and whether the
// whole bar is tucked into the edge tab. Per-session by design: a fresh boot
// starts with the bar visible and all stacks closed.
interface HudDockStore {
  openSection: string | null;
  tucked: boolean;
  toggleSection: (id: string) => void;
  closeSections: () => void;
  setTucked: (tucked: boolean) => void;
}

export const useHudDockStore = create<HudDockStore>((set, get) => ({
  openSection: null,
  tucked: false,
  toggleSection: (id) =>
    set({ openSection: get().openSection === id ? null : id }),
  closeSections: () => {
    if (get().openSection !== null) set({ openSection: null });
  },
  setTucked: (tucked) => set({ tucked, openSection: null }),
}));
