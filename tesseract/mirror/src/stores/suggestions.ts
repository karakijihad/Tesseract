import { create } from 'zustand';
import type { MemorySuggestionData } from '../lib/types';

export interface SuggestionEntry extends MemorySuggestionData {
  timestamp: string;
}

const MAX_SUGGESTIONS = 20;

interface SuggestionsState {
  suggestions: SuggestionEntry[];
  push: (entry: SuggestionEntry) => void;
  dismiss: (observation_id: string) => void;
  reset: () => void;
}

// Per-session observer suggestions — not persisted. The Observer panel
// shows what fired this session; cleared on session_reset / session_loaded
// by `dispatch.ts`. Suggestions are one-shot at the brain layer (drained
// by `_drain_pending_suggestions` and never re-injected), so the UI log
// is purely audit, not pending state.
export const useSuggestionsStore = create<SuggestionsState>()((set) => ({
  suggestions: [],

  push: (entry) =>
    set((state) => {
      if (state.suggestions.some((s) => s.observation_id === entry.observation_id)) {
        return state;
      }
      return {
        suggestions: [...state.suggestions.slice(-(MAX_SUGGESTIONS - 1)), entry],
      };
    }),

  dismiss: (observation_id) =>
    set((state) => ({
      suggestions: state.suggestions.filter((s) => s.observation_id !== observation_id),
    })),

  reset: () => set({ suggestions: [] }),
}));
