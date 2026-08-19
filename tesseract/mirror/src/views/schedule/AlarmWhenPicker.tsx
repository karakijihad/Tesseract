import { useEffect, useMemo, useState } from 'react';

import { Segmented } from '../../components/common/Segmented';
import { Button } from '../../components/common/Button';
import { IntervalCell } from './IntervalCell';

/**
 * Picker for alarm `when` strings — same shape as CadencePicker but the
 * grammar maps to `parse_alarm_spec` (one-shot, every-N, daily HH:MM,
 * weekly DAY HH:MM). Output strings are the canonical inputs accepted by
 * the parser, e.g. `1h30m`, `every 30m`, `daily 09:00`, `every monday 10:00`.
 *
 * Cron is intentionally absent — alarms ride the recurrence engine, not
 * cron. Operators who need full cron should use the scheduler instead.
 */

type AlarmMode = 'once' | 'interval' | 'daily' | 'weekly';

interface IntervalFields {
  hours: number;
  minutes: number;
  seconds: number;
}

interface ClockFields {
  hour: number;
  minute: number;
}

interface WeeklyFields {
  weekday: number; // 0 = Mon, 6 = Sun (matches _WEEKDAY_MAP in alarm_parser.py)
  hour: number;
  minute: number;
}

const MODE_LABELS: Record<AlarmMode, string> = {
  once: 'ONCE',
  interval: 'EVERY',
  daily: 'DAILY',
  weekly: 'WEEKLY',
};

const MODE_HINTS: Record<AlarmMode, string> = {
  once: 'One-shot — fires N hours/minutes/seconds from now, then disappears.',
  interval: 'Repeats every N hours/minutes/seconds. First fire is one interval from now.',
  daily: 'One fire per day at a fixed wall-clock time. Good for morning reminders.',
  weekly: 'One fire per week on a specific weekday at a fixed wall-clock time.',
};

const WEEKDAY_NAMES = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday'];
const WEEKDAY_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

function _formatInterval(f: IntervalFields): string {
  const parts: string[] = [];
  if (f.hours > 0) parts.push(`${f.hours}h`);
  if (f.minutes > 0) parts.push(`${f.minutes}m`);
  if (f.seconds > 0) parts.push(`${f.seconds}s`);
  return parts.join('');
}

function _intervalSeconds(f: IntervalFields): number {
  return f.hours * 3600 + f.minutes * 60 + f.seconds;
}

function _formatClock(f: ClockFields): string {
  return `${String(f.hour).padStart(2, '0')}:${String(f.minute).padStart(2, '0')}`;
}

function _humanizeSeconds(secs: number): string {
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) {
    const m = Math.floor(secs / 60);
    const s = secs % 60;
    return s ? `${m}m${s}s` : `${m}m`;
  }
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs - h * 3600) / 60);
  return m ? `${h}h${m}m` : `${h}h`;
}

interface Props {
  value: string;
  onCommit: (next: string) => void;
  onCancel: () => void;
  /** When true, hide save/cancel and emit `onCommit` on every state
   *  change. Used by AddAlarmForm to live-reflect picker state into
   *  the form's draft. The form owns the actual submit button. */
  embedded?: boolean;
}

export function AlarmWhenPicker({ value, onCommit, onCancel, embedded = false }: Props) {
  // We intentionally do NOT round-trip-parse `value` — the picker is only
  // shown when the operator clicks `+ add`. Pre-filling from a saved string
  // would mean re-implementing the Python parser in TS; not worth it for
  // the create flow.
  const [mode, setMode] = useState<AlarmMode>('once');
  const [interval, setInterval] = useState<IntervalFields>({ hours: 0, minutes: 30, seconds: 0 });
  const [daily, setDaily] = useState<ClockFields>({ hour: 9, minute: 0 });
  const [weekly, setWeekly] = useState<WeeklyFields>({ weekday: 0, hour: 9, minute: 0 });
  // Initial-value note: avoid the "stick" lint warning for unused setters.
  void value;

  const { whenString, validationError } = useMemo<{
    whenString: string;
    validationError: string | null;
  }>(() => {
    if (mode === 'once') {
      const secs = _intervalSeconds(interval);
      if (secs <= 0) return { whenString: '', validationError: 'set at least one non-zero unit' };
      return { whenString: _formatInterval(interval), validationError: null };
    }
    if (mode === 'interval') {
      const secs = _intervalSeconds(interval);
      if (secs <= 0) return { whenString: '', validationError: 'set at least one non-zero unit' };
      return { whenString: `every ${_formatInterval(interval)}`, validationError: null };
    }
    if (mode === 'daily') {
      return { whenString: `daily ${_formatClock(daily)}`, validationError: null };
    }
    return {
      whenString: `every ${WEEKDAY_NAMES[weekly.weekday]} ${_formatClock({ hour: weekly.hour, minute: weekly.minute })}`,
      validationError: null,
    };
  }, [mode, interval, daily, weekly]);

  const previewLabel = useMemo(() => {
    if (validationError) return validationError;
    if (mode === 'once') {
      return `fires in ${_humanizeSeconds(_intervalSeconds(interval))}`;
    }
    if (mode === 'interval') {
      return `repeats every ${_humanizeSeconds(_intervalSeconds(interval))}`;
    }
    if (mode === 'daily') {
      return `runs once per day at ${_formatClock(daily)}`;
    }
    return `runs every ${WEEKDAY_SHORT[weekly.weekday]} at ${_formatClock({ hour: weekly.hour, minute: weekly.minute })}`;
  }, [mode, validationError, interval, daily, weekly]);

  useEffect(() => {
    if (embedded) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel();
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [onCancel, embedded]);

  // Embedded mode: live-reflect each valid state into the parent's draft.
  // Empty string is also pushed when validation fails so the parent can
  // disable its submit button accordingly.
  useEffect(() => {
    if (embedded) onCommit(validationError ? '' : whenString);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [whenString, validationError, embedded]);

  const handleSave = () => {
    if (validationError || !whenString) return;
    onCommit(whenString);
  };

  return (
    <div className="cadence-picker" role="group" aria-label="Edit alarm time">
      <Segmented
        items={(Object.keys(MODE_LABELS) as AlarmMode[]).map((m) => ({
          key: m,
          label: MODE_LABELS[m],
          hint: MODE_HINTS[m],
        }))}
        value={mode}
        onSelect={setMode}
        label="Alarm shape"
      />

      {(mode === 'once' || mode === 'interval') && (
        <div className="cadence-picker-interval" aria-label="Duration fields">
          <IntervalCell label="H" max={23} value={interval.hours}
            onChange={(v) => setInterval({ ...interval, hours: v })} />
          <IntervalCell label="M" max={59} value={interval.minutes}
            onChange={(v) => setInterval({ ...interval, minutes: v })} />
          <IntervalCell label="S" max={59} value={interval.seconds}
            onChange={(v) => setInterval({ ...interval, seconds: v })} />
        </div>
      )}

      {mode === 'daily' && (
        <div className="cadence-picker-daily" aria-label="Daily time fields">
          <IntervalCell label="H" max={23} value={daily.hour}
            onChange={(v) => setDaily({ ...daily, hour: v })} />
          <span className="cadence-picker-colon">:</span>
          <IntervalCell label="M" max={59} value={daily.minute}
            onChange={(v) => setDaily({ ...daily, minute: v })} />
          <span className="cadence-picker-daily-hint">runs once per day at this time</span>
        </div>
      )}

      {mode === 'weekly' && (
        <div className="alarm-picker-weekly" aria-label="Weekly time fields">
          <Segmented
            items={WEEKDAY_SHORT.map((name, idx) => ({ key: idx, label: name }))}
            value={weekly.weekday}
            onSelect={(idx) => setWeekly({ ...weekly, weekday: idx })}
            label="Day of the week"
          />
          <div className="cadence-picker-daily">
            <IntervalCell label="H" max={23} value={weekly.hour}
              onChange={(v) => setWeekly({ ...weekly, hour: v })} />
            <span className="cadence-picker-colon">:</span>
            <IntervalCell label="M" max={59} value={weekly.minute}
              onChange={(v) => setWeekly({ ...weekly, minute: v })} />
          </div>
        </div>
      )}

      <div className="cadence-picker-preview">
        <span className={`cadence-picker-preview-text${validationError ? ' is-error' : ''}`}>
          {previewLabel}
        </span>
        {whenString && !validationError && (
          <span className="cadence-picker-preview-string">→ {whenString}</span>
        )}
      </div>

      {!embedded && (
        <div className="cadence-picker-actions">
          <Button onClick={onCancel}>cancel</Button>
          <Button
            tone="primary"
            onClick={handleSave}
            disabled={!!validationError || !whenString}
          >
            save
          </Button>
        </div>
      )}
    </div>
  );
}
