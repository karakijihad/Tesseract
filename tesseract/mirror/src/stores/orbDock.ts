import { create } from 'zustand';

interface OrbDockStore {
  dockEl: HTMLDivElement | null;
  setDockEl: (el: HTMLDivElement | null) => void;
}

export const useOrbDockStore = create<OrbDockStore>((set) => ({
  dockEl: null,
  setDockEl: (el) => set({ dockEl: el }),
}));
