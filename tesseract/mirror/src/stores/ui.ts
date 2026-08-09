import { create } from 'zustand';

export type View =
  | 'autonomy'
  | 'orb'
  | 'chat'
  | 'terminal'
  | 'pulse'
  | 'identity'
  | 'schedule'
  | 'agents'
  | 'conscience'
  | 'channels'
  | 'workspace'
  | 'settings';

interface UIState {
  view: View;
  drawerOpen: boolean;
  pendingStatsToast: boolean;
  setView: (view: View) => void;
  setDrawerOpen: (open: boolean) => void;
  toggleDrawer: () => void;
  setPendingStatsToast: (pending: boolean) => void;
}

// The right-side session drawer is the only overlay drawer now — the spawn
// transcript moved to a canvas card (D-6, canvas/delegateTranscript.ts).
export const useUIStore = create<UIState>((set) => ({
  // SC-2 — the spatial cockpit boots to the bare orb home (`agent`): no panel
  // summoned, the orb glowing on a clean stage, the home tab active. Every view
  // (incl. the AU-7 autonomy dashboard) is one tab-click away as a glass panel.
  // `view` tracks the focused panel, or `agent` when none are open.
  view: 'orb',
  drawerOpen: false,
  pendingStatsToast: false,
  setView: (view) => set({ view }),
  setDrawerOpen: (open) => set({ drawerOpen: open }),
  toggleDrawer: () => set((state) => ({ drawerOpen: !state.drawerOpen })),
  setPendingStatsToast: (pending) => set({ pendingStatsToast: pending }),
}));
