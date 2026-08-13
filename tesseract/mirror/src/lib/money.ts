// One answer for money on screen. Three existed: two `formatUsd`s that
// disagreed about what to do with null, and two copies of the same band
// thresholds. A figure that reads `$1.4` in one panel and `$1.42` in the next
// looks like two different numbers.

/**
 * Spend, at the precision the figure deserves. Large totals lose the cents
 * they cannot meaningfully carry; small ones keep them.
 */
export function formatUsd(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  if (n >= 100) return `$${n.toFixed(0)}`;
  if (n >= 10) return `$${n.toFixed(1)}`;
  return `$${n.toFixed(2)}`;
}

/** How close to a ceiling counts as fine, worth noticing, or nearly spent. */
export function colorBand(ratio: number): "ok" | "warn" | "bad" {
  if (ratio < 0.6) return "ok";
  if (ratio < 0.85) return "warn";
  return "bad";
}
