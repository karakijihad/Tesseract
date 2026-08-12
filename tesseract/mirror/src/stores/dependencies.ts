import { create } from "zustand";

import { fetchDependencies, type DependencyAdvice, type DependencyAttention } from "../lib/api";

// The launch pass's verdict, for the surfaces that report it.
//
// Its own store rather than a slice of `update`: the two answer different
// questions with different remedies — "there is a newer TESSERACT" is an
// action the operator takes, while "something this build needs is missing or
// is the wrong version" is usually already being repaired at launch. Sharing
// a store would make the HUD chip logic have to tell them apart anyway.
interface DependencyStoreState {
  attention: DependencyAttention[];
  advice: DependencyAdvice[];
  checkedAt: string | null;
  loading: boolean;
  error: string | null;
  // Only the ones a person should act on. `attention` from the backend is
  // already filtered by consent — a lane the operator declined is not a
  // problem, it is their answer being honoured.
  count: () => number;
  // Whether anything is the WRONG version, as opposed to merely missing. The
  // distinction decides the chip's wording: a stale artifact is producing
  // wrong behaviour right now, where an absent one is a capability that is
  // simply off.
  hasDrift: () => boolean;
  hydrate: () => Promise<void>;
}

export const useDependencyStore = create<DependencyStoreState>((set, get) => ({
  attention: [],
  advice: [],
  checkedAt: null,
  loading: false,
  error: null,

  count: () => get().attention.length,
  hasDrift: () => get().attention.some((d) => d.state === "stale"),

  hydrate: async () => {
    set({ loading: true });
    try {
      const report = await fetchDependencies();
      set({
        attention: report.attention,
        advice: report.advice,
        checkedAt: report.checked_at,
        loading: false,
        error: null,
      });
    } catch (err) {
      // Never blanks what is already on screen. This is a background read of
      // an artifact that may legitimately not exist yet (an install that has
      // not reconciled once), and a failed read is not news.
      set({
        loading: false,
        error: err instanceof Error ? err.message : "could not read dependencies",
      });
    }
  },
}));
