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


/** How long until a moment still ahead of us, in the words a person reads.
 *
 *  `clock` is for a stamp on something that already happened: it gives the
 *  time of day only when the date is today, and a bare day and month
 *  otherwise. Pointed at the future that renders as "lifts itself at 8 Sep",
 *  which is not a time, is not grammatical, and leaves the reader unable to
 *  tell the start of that day from thirty hours away. A wait is a duration,
 *  so this says one.
 *
 *  Past moments and unparseable input both return the empty string, so a
 *  caller can fall back to whatever it says when there is nothing to say. */
export function until(iso: string | null | undefined): string {
  if (!iso) return '';
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return '';
  const seconds = Math.floor((at - Date.now()) / 1000);
  if (seconds <= 0) return '';
  const min = Math.floor(seconds / 60);
  if (min < 60) return `in ${Math.max(min, 1)} minutes`;
  const hr = Math.floor(min / 60);
  if (hr < 48) return `in ${hr} hour${hr === 1 ? '' : 's'}`;
  return `in ${Math.floor(hr / 24)} days`;
}


/** A stamp as a person reads it on a dense row: the clock for today, the day
 *  and month for anything older. Lives here rather than in whichever room
 *  needed it first, which is where five surfaces had been importing it from. */
export function clock(iso: string | null | undefined): string {
  if (!iso) return '';
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return '';
  const now = new Date();
  if (at.toDateString() === now.toDateString()) return at.toTimeString().slice(0, 5);
  return at.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}


/** When a reading was taken, and whether it still stands.
 *
 * A number with no age reads as a number about now. `expectedWithin` is the
 * producer's own cadence in seconds, from the row's own declaration, so this
 * says how old a reading is against what it promised rather than against a
 * threshold written here. Past its own cadence it says so; past twice it says
 * the reading is no longer current, which is what the room's `unknown` means.
 *
 * The words are deliberately about the READING and not about the machine: a
 * sweep being late says nothing about whether anything is wrong, and this
 * surface has been reprimanded for confusing the two.
 */
export function freshness(
  iso: string | null | undefined,
  expectedWithin: number | null | undefined,
): string {
  if (!iso) return '';
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return '';
  const taken = clock(iso);
  if (!expectedWithin || expectedWithin <= 0) return taken;
  const late = Math.floor((Date.now() - at) / 1000) - expectedWithin;
  if (late <= 0) return `${taken}, every ${inWords(expectedWithin)}`;
  return `${taken}, ${inWords(late)} late`;
}

/** A span of seconds as a person says it. Whole units only: a row is 31 pixels
 *  tall and "1h 4m 12s" is not what anybody reads off one. */
export function inWords(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  if (whole < 60) return `${whole}s`;
  const min = Math.round(whole / 60);
  if (min < 60) return `${min}m`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h`;
  return `${Math.round(hr / 24)}d`;
}
