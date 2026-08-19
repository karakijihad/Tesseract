import { useCallback, useEffect, useState } from "react";

import { useWebSocketStore } from "../stores/websocket";
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

  const set = useCallback(
    (value: T) => {
      CACHE.set(key, value);
      setData(value);
    },
    [key],
  );

  useEffect(() => {
    let cancelled = false;
    setError(null);
    fetcher()
      .then((value) => {
        // Cache FIRST, unconditionally. Guarding this behind `cancelled` meant
        // a section switched away from before its fetch landed never cached at
        // all — and StrictMode double-mounts, so in dev the first mount always
        // cancelled and the cache was never populated by anything. Only the
        // setState needs the guard; a response is worth keeping whoever asked.
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
  }, [key, wsGeneration, retryTick, nonce]);

  return {
    data,
    error,
    loading: data === null && error === null,
    set,
    setError,
    refresh: useCallback(() => setNonce((n) => n + 1), []),
  };
}
