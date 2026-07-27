/**
 * Format an ISO timestamp as a relative string ("just now", "3m ago", "5h ago").
 *
 * `fallback` is returned when iso is null or unparseable — surfaces vary:
 * right-panel header uses `'—'`, observer stats chip uses `'never'`, soul tab
 * uses `'—'`. Callers pass whichever semantic fits their empty state.
 *
 * Sub-minute resolution is reported in seconds ("29s ago") so a fast-polling
 * surface like ObserverStatsChip can show live progress without a dedicated
 * variant.
 */
export function formatRelative(iso: string | null | undefined, fallback: string = '—'): string {
  if (!iso) return fallback;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return fallback;
  const delta = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (delta < 60) return `${delta}s ago`;
  const min = Math.floor(delta / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  return `${day}d ago`;
}
