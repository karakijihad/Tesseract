import { create } from 'zustand';

export type View =
  | 'autonomy'
  | 'orb'
  | 'chat'
  | 'terminal'
  | 'pulse'
  | 'identity'
  | 'conscience'
  | 'channels'
  | 'workspace'
  | 'graph'
  | 'settings';

interface UIState {
  view: View;
  chatRailOpen: boolean;
  pendingStatsToast: boolean;
  /** A section the assistant asked for, waiting for the RailView that owns
   *  that key to pick it up. The command names a view and a row; only the
   *  view is a store the app already had, so the row rides here until the
   *  panel it belongs to is mounted and can consume it. Cleared on consume,
   *  so it moves one rail once rather than pinning it.
   *
   *  **It carries the view, not just the row.** Section keys are not unique
   *  across panels, and a bare string was taken by whichever rail mounted
   *  first and happened to have a row of that name. Asking for one panel's
   *  section moved another panel's rail, silently, and the panel that was
   *  actually summoned opened wherever it had been left. */
  requestedSection: { view: View; section: string } | null;
  setView: (view: View) => void;
  setChatRailOpen: (open: boolean) => void;
  toggleChatRail: () => void;
  setPendingStatsToast: (pending: boolean) => void;
  /** Set by the agent-driven path (cockpit_show) alongside `openPanel`, so
   *  the panel is summoned exactly the way the tab summons it. */
  setRequestedSection: (view: View, section: string | null) => void;
  takeRequestedSection: () => void;
}

// The conversations rail lives inside the chat view and is open by default;
// hiding it is the operator's own act, and an edge tab brings it back.
export const useUIStore = create<UIState>((set) => ({
  // SC-2 — the spatial cockpit boots to the bare orb home (`agent`): no panel
  // summoned, the orb glowing on a clean stage, the home tab active. Every view
  // (incl. the AU-7 autonomy dashboard) is one tab-click away as a glass panel.
  // `view` tracks the focused panel, or `agent` when none are open.
  view: 'orb',
  chatRailOpen: true,
  pendingStatsToast: false,
  requestedSection: null,
  setView: (view) => set({ view }),
  setChatRailOpen: (open) => set({ chatRailOpen: open }),
  toggleChatRail: () => set((state) => ({ chatRailOpen: !state.chatRailOpen })),
  setPendingStatsToast: (pending) => set({ pendingStatsToast: pending }),
  setRequestedSection: (view, section) =>
    set({ requestedSection: section ? { view, section } : null }),
  takeRequestedSection: () => set({ requestedSection: null }),
}));
