import { useEffect, useState } from "react";

import { useWebSocketStore } from "../stores/websocket";

// Self-healing for section fetches (2026-07-30): while `active` (a fetch
// error is showing) AND the backend connection is up, the returned tick
// increments every `intervalMs` — include it in the fetch effect's deps
// and the section retries until it succeeds. Idle (no error, or backend
// genuinely down) costs nothing; reconnects are already covered by the
// WS `generation` counter.
export function useFetchRetryTick(
  active: boolean,
  intervalMs = 10_000,
): number {
  const [tick, setTick] = useState(0);
  const wsStatus = useWebSocketStore((s) => s.status);

  useEffect(() => {
    if (!active || wsStatus !== "connected") return;
    const id = window.setInterval(() => setTick((t) => t + 1), intervalMs);
    return () => window.clearInterval(id);
  }, [active, wsStatus, intervalMs]);

  return tick;
}
