import { create } from 'zustand';

interface RoutingState {
  role: string | null;
  provider: string | null;
  model: string | null;
  taskClass: string | null;
  setRouting: (r: { role: string; provider: string; model: string; taskClass: string }) => void;
  clear: () => void;
}

export const useRoutingStore = create<RoutingState>((set) => ({
  role: null,
  provider: null,
  model: null,
  taskClass: null,
  setRouting: ({ role, provider, model, taskClass }) =>
    set({ role, provider, model, taskClass }),
  clear: () => set({ role: null, provider: null, model: null, taskClass: null }),
}));
