import { useCallback, useEffect, useState } from "react";

import { useWebSocketStore } from "../stores/websocket";
import { useStaleStore } from "../stores/stale";
import { useFetchRetryTick } from "./useFetchRetry";

/** Last good value per key, outliving the component that fetched it.
 *
 * Settings sections used to be mounted all at once in one long column, so each
 * fetched once when the panel opened. The rail mounts one section at a time, so
 * switching rows unmounts a section and takes its `useState` with it — coming
 * back showed `(loading…)` and refetched from zero. The data has not changed in
 * the two seconds you were elsewhere; the spinner was the only new information.
 */
const CACHE = new Map<string, unknown>();

/** Which fetch for a key is the current one.
 *
 * The cache is written unconditionally, deliberately (see the effect below).
 * That leaves one hole: a fetch started BEFORE a refresh can resolve after
 * it, and its older answer then overwrites the newer one in the cache, so the
 * next remount paints what the refresh had just replaced. Counting the fetches
 * per key closes it without giving up the unconditional write, because a
 * superseded fetch can be recognised rather than merely cancelled. */
const GENERATION = new Map<string, number>();

interface CachedFetch<T> {
  /** The cached value on a revisit, so the section paints immediately. */
  data: T | null;
  error: string | null;
  /** True only while there is nothing to show — never during a revalidate. */
  loading: boolean;
  /** Replace the cached value after a mutation returns fresh state. */
  set: (value: T) => void;
  /** One error channel, shared with the fetch — a section's save failure and
   *  its load failure render in the same place, as they did before. */
  setError: (message: string | null) => void;
  refresh: () => void;
}

export function useCachedFetch<T>(
  key: string,
  fetcher: () => Promise<T>,
): CachedFetch<T> {
  const [data, setData] = useState<T | null>(
    () => (CACHE.get(key) as T | undefined) ?? null,
  );
  const [error, setError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  // Re-runs on every WS (re)connection: a backend restart must replace a
  // pre-restart "Failed to fetch" with fresh data (2026-07-30).
  const wsGeneration = useWebSocketStore((s) => s.generation);
  const retryTick = useFetchRetryTick(error !== null);
  // Bumped when the backend says this key is out of date. A section that is
  // not mounted misses nothing: it refetches on its next mount anyway.
  const staleTick = useStaleStore((s) => s.ticks[key] ?? 0);

  const set = useCallback(
    (value: T) => {
      // Retires whatever is in flight. A caller reaches for this after its own
      // mutation came back with fresh state, which is newer than anything a
      // fetch started before it can return; without the bump that older fetch
      // still counts as current and overwrites what was just saved.
      GENERATION.set(key, (GENERATION.get(key) ?? 0) + 1);
      CACHE.set(key, value);
      setData(value);
    },
    [key],
  );

  useEffect(() => {
    let cancelled = false;
    const generation = (GENERATION.get(key) ?? 0) + 1;
    GENERATION.set(key, generation);
    setError(null);
    fetcher()
      .then((value) => {
        // Cache unconditionally, but only if nothing newer has been asked for.
        // Guarding this behind `cancelled` meant a section switched away from
        // before its fetch landed never cached at all — and StrictMode
        // double-mounts, so in dev the first mount always cancelled and the
        // cache was never populated by anything. A response is worth keeping
        // whoever asked for it; it is not worth keeping once a later request
        // for the same key exists, because that one knows something this one
        // does not.
        if (GENERATION.get(key) !== generation) return;
        CACHE.set(key, value);
        if (cancelled) return;
        setData(value);
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      cancelled = true;
    };
    // `fetcher` is re-created per render by every caller; keying the effect on
    // it would refetch forever. The key is what identifies the request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, wsGeneration, retryTick, staleTick, nonce]);

  return {
    data,
    error,
    loading: data === null && error === null,
    set,
    setError,
    refresh: useCallback(() => setNonce((n) => n + 1), []),
  };
}
