import { create } from "zustand";

/** Which cached fetches the backend has said are out of date.
 *
 * `useCachedFetch` keeps the last good value for a key so switching between
 * settings rows repaints instantly instead of flashing a spinner over data
 * that has not changed. The cost of that is a panel showing what it fetched
 * on mount forever. When something else writes what the panel is showing, it
 * says so here, and the hook holding that key refetches.
 *
 * A counter per key rather than a flag: two writes in a row are two nudges,
 * and a flag would have to be cleared by whoever consumed it, which is a
 * question about who is mounted that this store cannot answer.
 */
interface StaleState {
  ticks: Record<string, number>;
  markStale: (key: string) => void;
}

export const useStaleStore = create<StaleState>((set) => ({
  ticks: {},
  markStale: (key) =>
    set((s) => ({ ticks: { ...s.ticks, [key]: (s.ticks[key] ?? 0) + 1 } })),
}));
