import { create } from "zustand";

// Bottom HUD dock state: whether the whole bar is tucked into its edge tab.
// Per-session by design — a fresh boot starts with the bar visible.
//
// `openSection` went with the section stacks (operator, 2026-08-13): every tab
// is on the bar now, so there is no stack to have one of open.
interface HudDockStore {
  tucked: boolean;
  setTucked: (tucked: boolean) => void;
}

export const useHudDockStore = create<HudDockStore>((set) => ({
  tucked: false,
  setTucked: (tucked) => set({ tucked }),
}));
