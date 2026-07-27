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
  if (f.days > 365) return 'days must be 0–365';
  if (f.hours > 23) return 'hours must be 0–23';
  if (f.minutes > 59) return 'minutes must be 0–59';
  if (f.seconds > 59) return 'seconds must be 0–59';
  if (intervalToSeconds(f) === 0) return 'interval must be non-zero';
  return null;
}

export function validateDaily(f: DailyFields): string | null {
  if (f.hour < 0 || f.hour > 23) return 'hour must be 0–23';
  if (f.minute < 0 || f.minute > 59) return 'minute must be 0–59';
  return null;
}

export function validateCron(f: CronFields): string | null {
  const fields: [string, string, number, number][] = [
    [f.minute, 'minute', 0, 59],
    [f.hour, 'hour', 0, 23],
    [f.dom, 'dom', 1, 31],
    [f.month, 'month', 1, 12],
    [f.dow, 'dow', 0, 6],
  ];
  for (const [field, label, min, max] of fields) {
    if (!field || !isValidCronField(field, min, max)) return `invalid ${label} field: ${field}`;
  }
  return null;
}

function isValidCronField(field: string, min: number, max: number): boolean {
  if (field === '*') return true;
  for (const part of field.split(',')) {
    if (!isValidCronPart(part, min, max)) return false;
  }
  return true;
}

function isValidCronPart(part: string, min: number, max: number): boolean {
  const stepMatch = /^(.+)\/(\d+)$/.exec(part);
  if (stepMatch) {
    const step = parseInt(stepMatch[2], 10);
    if (step <= 0) return false;
    return stepMatch[1] === '*' || isValidCronPart(stepMatch[1], min, max);
  }
  const rangeMatch = /^(\d+)-(\d+)$/.exec(part);
  if (rangeMatch) {
    const lo = parseInt(rangeMatch[1], 10);
    const hi = parseInt(rangeMatch[2], 10);
    return lo >= min && hi <= max && lo <= hi;
  }
  if (/^\d+$/.test(part)) {
    const v = parseInt(part, 10);
    return v >= min && v <= max;
  }
  return false;
}

// Compute next fire time for a cadence string. Interval shorthand measures
// forward from `reference` (defaults to `now`); cron walks minute-by-minute
// up to 366 days ahead. Returns null if unparseable or no match in window.
export function nextFireTime(value: string, now: Date = new Date(), reference?: Date): Date | null {
  const parsed = parseCadence(value);
  if (parsed.mode === 'interval') {
    const seconds = intervalToSeconds(parsed.fields);
    if (seconds <= 0) return null;
    const base = reference ?? now;
    return new Date(base.getTime() + seconds * 1000);
  }
  const cronFields: CronFields = parsed.mode === 'daily'
    ? { minute: String(parsed.fields.minute), hour: String(parsed.fields.hour), dom: '*', month: '*', dow: '*' }
    : parsed.fields;
  const dt = new Date(now.getTime());
  dt.setSeconds(0, 0);
  dt.setMinutes(dt.getMinutes() + 1);
  for (let i = 0; i < 366 * 24 * 60; i++) {
    if (cronMatch(cronFields, dt)) return new Date(dt.getTime());
    dt.setMinutes(dt.getMinutes() + 1);
  }
  return null;
}

function cronMatch(f: CronFields, dt: Date): boolean {
  return fieldMatches(f.minute, dt.getMinutes(), 0, 59)
    && fieldMatches(f.hour, dt.getHours(), 0, 23)
    && fieldMatches(f.dom, dt.getDate(), 1, 31)
    && fieldMatches(f.month, dt.getMonth() + 1, 1, 12)
    && fieldMatches(f.dow, dt.getDay(), 0, 6);
}

function fieldMatches(field: string, value: number, min: number, max: number): boolean {
  if (field === '*') return true;
  for (const part of field.split(',')) {
    if (partMatches(part, value, min, max)) return true;
  }
  return false;
}

function partMatches(part: string, value: number, min: number, max: number): boolean {
  const stepMatch = /^(.+)\/(\d+)$/.exec(part);
  if (stepMatch) {
    const step = parseInt(stepMatch[2], 10);
    const base = stepMatch[1];
    let lo = min;
    let hi = max;
    if (base !== '*') {
      const rangeMatch = /^(\d+)-(\d+)$/.exec(base);
      if (rangeMatch) { lo = parseInt(rangeMatch[1], 10); hi = parseInt(rangeMatch[2], 10); }
      else if (/^\d+$/.test(base)) { lo = parseInt(base, 10); hi = max; }
    }
    if (value < lo || value > hi) return false;
    return (value - lo) % step === 0;
  }
  const rangeMatch = /^(\d+)-(\d+)$/.exec(part);
  if (rangeMatch) {
    return value >= parseInt(rangeMatch[1], 10) && value <= parseInt(rangeMatch[2], 10);
  }
  if (/^\d+$/.test(part)) {
    return parseInt(part, 10) === value;
  }
  return false;
}

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
