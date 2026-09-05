// Cadence parser / formatter / next-fire preview for the Schedule tab's
// CadencePicker. Mirrors the backend grammar in `tesseract/scheduler/engine.py`:
//   - Interval shorthand: `{N}d{N}h{N}m{N}s` (at least one unit, compound OK).
//   - Cron: standard 5-field (min hour dom month dow).

export type CadenceMode = 'interval' | 'daily' | 'cron';

export interface IntervalFields {
  days: number;
  hours: number;
  minutes: number;
  seconds: number;
}

export interface DailyFields {
  hour: number;
  minute: number;
}

export interface CronFields {
  minute: string;
  hour: string;
  dom: string;
  month: string;
  dow: string;
}

export type ParsedCadence =
  | { mode: 'interval'; fields: IntervalFields }
  | { mode: 'daily'; fields: DailyFields }
  | { mode: 'cron'; fields: CronFields };

const INTERVAL_RE = /^\s*(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?\s*$/;
const DAILY_CRON_RE = /^\s*(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*\s*$/;

export function parseCadence(value: string): ParsedCadence {
  const raw = value.trim();

  const intervalMatch = INTERVAL_RE.exec(raw);
  if (intervalMatch && intervalMatch.slice(1).some(Boolean)) {
    return {
      mode: 'interval',
      fields: {
        days: intervalMatch[1] ? parseInt(intervalMatch[1], 10) : 0,
        hours: intervalMatch[2] ? parseInt(intervalMatch[2], 10) : 0,
        minutes: intervalMatch[3] ? parseInt(intervalMatch[3], 10) : 0,
        seconds: intervalMatch[4] ? parseInt(intervalMatch[4], 10) : 0,
      },
    };
  }

  const dailyMatch = DAILY_CRON_RE.exec(raw);
  if (dailyMatch) {
    return {
      mode: 'daily',
      fields: { hour: parseInt(dailyMatch[2], 10), minute: parseInt(dailyMatch[1], 10) },
    };
  }

  const parts = raw.split(/\s+/);
  if (parts.length === 5) {
    return {
      mode: 'cron',
      fields: {
        minute: parts[0],
        hour: parts[1],
        dom: parts[2],
        month: parts[3],
        dow: parts[4],
      },
    };
  }

  return { mode: 'cron', fields: { minute: '*', hour: '*', dom: '*', month: '*', dow: '*' } };
}

export function formatInterval(f: IntervalFields): string {
  const parts: string[] = [];
  if (f.days) parts.push(`${f.days}d`);
  if (f.hours) parts.push(`${f.hours}h`);
  if (f.minutes) parts.push(`${f.minutes}m`);
  if (f.seconds) parts.push(`${f.seconds}s`);
  return parts.join('');
}

export function formatDaily(f: DailyFields): string {
  return `${f.minute} ${f.hour} * * *`;
}

export function formatCron(f: CronFields): string {
  return `${f.minute} ${f.hour} ${f.dom} ${f.month} ${f.dow}`.trim();
}

export function intervalToSeconds(f: IntervalFields): number {
  return f.days * 86400 + f.hours * 3600 + f.minutes * 60 + f.seconds;
}

export function validateInterval(f: IntervalFields): string | null {
  if (f.days < 0 || f.hours < 0 || f.minutes < 0 || f.seconds < 0) return 'negative values not allowed';
  if (f.days > 365) return 'days must be 0 to 365';
  if (f.hours > 23) return 'hours must be 0 to 23';
  if (f.minutes > 59) return 'minutes must be 0 to 59';
  if (f.seconds > 59) return 'seconds must be 0 to 59';
  if (intervalToSeconds(f) === 0) return 'interval must be non-zero';
  return null;
}

export function validateDaily(f: DailyFields): string | null {
  if (f.hour < 0 || f.hour > 23) return 'hour must be 0 to 23';
  if (f.minute < 0 || f.minute > 59) return 'minute must be 0 to 59';
  return null;
}

// The cron grammar is NOT read here. `GET /api/schedule/cadence` answers
// whether a cadence is valid, what it says and when it next fires, from the
// croniter the scheduler itself runs on. This file used to answer all three
// with a hand-written parser and a hand-written matcher, and the two readings
// disagreed four separate times: named weekdays, named months, Sunday's
// second number, day-of-month required WITH day-of-week rather than either,
// and `?`, `L` and `#`. Each was a cadence the operator could not create and
// the runtime would have run. What is left here shapes the picker's own
// fields into a string and checks its own numeric inputs, which is a
// question about this form and not about the grammar.

export function humanizeDelta(ms: number): string {
  if (ms < 0) return 'past';
  const s = Math.round(ms / 1000);
  if (s < 60) return `in ${s}s`;
  const m = Math.floor(s / 60);
  const sr = s % 60;
  if (m < 60) return sr ? `in ${m}m ${sr}s` : `in ${m}m`;
  const h = Math.floor(m / 60);
  const mr = m % 60;
  if (h < 24) return mr ? `in ${h}h ${mr}m` : `in ${h}h`;
  const d = Math.floor(h / 24);
  const hr = h % 24;
  return hr ? `in ${d}d ${hr}h` : `in ${d}d`;
}
