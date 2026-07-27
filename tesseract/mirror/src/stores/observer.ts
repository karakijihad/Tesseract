/**
 * Observer store — Mirror-wide observer state machine.
 *
 * State machine:  off → armed → observing → off
 *
 *   off       Observer inactive, no snapshots.
 *   armed     Observer ready, no live panes yet — promotes to
 *             observing the moment any pane is open.
 *   observing Actively forwarding PTY chunks into the observer.
 *
 * Phase 6 (terminal-control 2026-05-16): observer is always-on by
 * default and the backend auto-grants consent for every live + future
 * pane on arm. There is no per-pane consent UI — the right-panel
 * arm/disarm toggle is the single operator control. Backend is
 * authoritative; arm/disarm POST to /api/observer/* and the JSON
 * response carries the resolved state so we don't drift.
 */

import { create } from 'zustand';
import { BACKEND_BASE } from '../lib/endpoints';

export type ObserverState = 'off' | 'armed' | 'observing';

interface ObserverStoreState {
  state: ObserverState;
  arm: () => Promise<void>;
  disarm: () => Promise<void>;
  syncFromBackend: () => Promise<void>;
}

const API_BASE = `${BACKEND_BASE}/api/observer`;

export const useObserverStore = create<ObserverStoreState>((set) => ({
  state: 'off',

  arm: async () => {
    try {
      const res = await fetch(`${API_BASE}/arm`, { method: 'POST' });
      if (res.ok) {
        const data = await res.json().catch(() => ({ state: 'armed' }));
        set({ state: (data.state as ObserverState) ?? 'armed' });
      }
    } catch (err) {
      console.error('Failed to arm observer:', err);
    }
  },

  disarm: async () => {
    try {
      const res = await fetch(`${API_BASE}/disarm`, { method: 'POST' });
      if (res.ok) {
        set({ state: 'off' });
      }
    } catch (err) {
      console.error('Failed to disarm observer:', err);
    }
  },

  syncFromBackend: async () => {
    try {
      const res = await fetch(`${API_BASE}/status`);
      if (res.ok) {
        const data = await res.json();
        set({ state: data.state as ObserverState });
      }
    } catch {
      // Backend not available — stay in current state
    }
  },
}));
