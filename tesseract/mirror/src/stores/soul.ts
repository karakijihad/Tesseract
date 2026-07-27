import { create } from 'zustand';
import { fetchSoul as apiFetchSoul } from '../lib/api';

interface SoulState {
  content: string;
  lastReflectedAt: string | null;
  setContent: (content: string) => void;
  setLastReflectedAt: (iso: string | null) => void;
  fetchSoul: () => Promise<void>;
  reset: () => void;
}

export const useSoulStore = create<SoulState>((set) => ({
  content: '',
  lastReflectedAt: null,

  setContent: (content) => set({ content }),
  setLastReflectedAt: (iso) => set({ lastReflectedAt: iso }),

  fetchSoul: async () => {
    try {
      const soul = await apiFetchSoul();
      set({
        content: soul.content,
        lastReflectedAt: soul.last_reflected_at ?? null,
      });
    } catch (err) {
      console.error('[soul] fetchSoul failed:', err);
    }
  },

  reset: () => set({ content: '', lastReflectedAt: null }),
}));
