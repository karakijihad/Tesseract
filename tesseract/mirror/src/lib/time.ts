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
 *  caller can fall back to whatever it says when there is nothing to say. The
 *  under-a-second case is one of those: it is not in the past, but a wait that
 *  short is over by the time the words are read.
 *
 *  Each unit is rounded to the nearest, not floored. Flooring is right for an
 *  age and wrong for a wait: it told a reader 29 hours for a wait of 29 hours
 *  and 59 minutes, which is the one reading that makes them plan for the wrong
 *  day. It also put the only correct answer for a whole-numbered wait on a
 *  boundary that one elapsed millisecond falls off, which a test measured at
 *  one run in six hundred.
 *
 *  Choosing the unit from the ROUNDED count and not the raw one is what keeps
 *  the scale continuous: 59 minutes and 40 seconds rounds to 60 minutes, which
 *  is not a thing this says, so it falls through and is answered in hours. */
export function until(iso: string | null | undefined): string {
  if (!iso) return '';
  const at = new Date(iso).getTime();
  if (Number.isNaN(at)) return '';
  const ms = at - Date.now();
  if (ms < 1000) return '';
  const said = (n: number, unit: string) => `in ${n} ${unit}${n === 1 ? '' : 's'}`;
  const min = Math.round(ms / 60_000);
  if (min < 60) return said(Math.max(min, 1), 'minute');
  const hr = Math.round(ms / 3_600_000);
  if (hr < 48) return said(hr, 'hour');
  return said(Math.round(ms / 86_400_000), 'day');
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
