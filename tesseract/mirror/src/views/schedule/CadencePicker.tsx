import { useEffect, useMemo, useState } from 'react';

import { Hint } from '../../components/ui/Hint';
import { IntervalCell } from './IntervalCell';
import {
  type CadenceMode,
  type CronFields,
  type DailyFields,
  type IntervalFields,
  formatCron,
  formatDaily,
  formatInterval,
  humanizeDelta,
  nextFireTime,
  parseCadence,
  validateCron,
  validateDaily,
  validateInterval,
} from './cadence';

interface Props {
  jobName: string;
  value: string;
  onCommit: (next: string) => void;
  onCancel: () => void;
  /** When true, hide save/cancel and emit `onCommit` on every state
   *  change. Used by AddJobForm to live-reflect picker state into the
   *  form's draft. The form owns the actual submit button. */
  embedded?: boolean;
}

const MODE_LABELS: Record<CadenceMode, string> = {
  interval: 'Interval',
  daily: 'Daily',
  cron: 'Cron',
};

const MODE_HINTS: Record<CadenceMode, string> = {
  interval: 'Every N units — fires N seconds/minutes/hours/days after its last run. Good for heartbeats and polling.',
  daily: 'Once per day at a fixed wall-clock time. Good for nightly rollups and morning digests.',
  cron: 'Full 5-field cron expression (min hour day-of-month month day-of-week). For anything irregular — weekdays only, every 15 min, 1st of month, etc.',
};

const CRON_PLACEHOLDERS: Record<keyof CronFields, string> = {
  minute: 'min',
  hour: 'hour',
  dom: 'dom',
  month: 'mon',
  dow: 'dow',
};

export function CadencePicker({ jobName, value, onCommit, onCancel, embedded = false }: Props) {
  const initial = useMemo(() => parseCadence(value), [value]);
  const [mode, setMode] = useState<CadenceMode>(initial.mode);
  const [interval, setInterval] = useState<IntervalFields>(() =>
    initial.mode === 'interval' ? initial.fields : { days: 0, hours: 0, minutes: 15, seconds: 0 },
  );
  const [daily, setDaily] = useState<DailyFields>(() =>
    initial.mode === 'daily' ? initial.fields : { hour: 9, minute: 0 },
  );
  const [cron, setCron] = useState<CronFields>(() =>
    initial.mode === 'cron'
      ? initial.fields
      : { minute: '0', hour: '*', dom: '*', month: '*', dow: '*' },
  );
  const { cadenceString, validationError } = useMemo(() => {
    if (mode === 'interval') {
      const err = validateInterval(interval);
      return { cadenceString: err ? '' : formatInterval(interval), validationError: err };
    }
    if (mode === 'daily') {
      const err = validateDaily(daily);
      return { cadenceString: err ? '' : formatDaily(daily), validationError: err };
    }
    const err = validateCron(cron);
    return { cadenceString: err ? '' : formatCron(cron), validationError: err };
  }, [mode, interval, daily, cron]);

  const previewLabel = useMemo(() => {
    if (validationError || !cadenceString) return validationError ?? 'enter a cadence';
    const next = nextFireTime(cadenceString);
    if (!next) return 'could not resolve next fire';
    const hhmmss = next.toLocaleTimeString([], { hour12: false });
    return `next: ${hhmmss} (${humanizeDelta(next.getTime() - Date.now())})`;
  }, [cadenceString, validationError]);

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
    if (embedded) onCommit(validationError ? '' : cadenceString);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cadenceString, validationError, embedded]);

  const handleSave = () => {
    if (validationError || !cadenceString) return;
    onCommit(cadenceString);
  };

  return (
    <div className="cadence-picker" role="group" aria-label={`Edit cadence for ${jobName}`}>
      <div className="cadence-picker-modes">
        {(Object.keys(MODE_LABELS) as CadenceMode[]).map((m) => (
          <Hint key={m} label={MODE_HINTS[m]} position="bottom" maxWidth={320}>
            <button
              type="button"
              className={`cadence-picker-mode${mode === m ? ' is-active' : ''}`}
              onClick={() => setMode(m)}
              aria-pressed={mode === m}
            >
              {MODE_LABELS[m]}
            </button>
          </Hint>
        ))}
      </div>

      {mode === 'interval' && (
        <div className="cadence-picker-interval" aria-label="Interval fields">
          <IntervalCell label="D" max={365} value={interval.days}
            onChange={(v) => setInterval({ ...interval, days: v })} />
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

      {mode === 'cron' && (
        <div className="cadence-picker-cron" aria-label="Cron fields">
          {(Object.keys(CRON_PLACEHOLDERS) as (keyof CronFields)[]).map((key) => (
            <label key={key} className="cadence-picker-cron-field">
              <span className="cadence-picker-cron-label">{CRON_PLACEHOLDERS[key]}</span>
              <input
                className="cadence-picker-cron-input"
                value={cron[key]}
                onChange={(e) => setCron({ ...cron, [key]: e.target.value })}
                placeholder={CRON_PLACEHOLDERS[key]}
                spellCheck={false}
                autoCapitalize="off"
              />
            </label>
          ))}
        </div>
      )}

      <div className="cadence-picker-preview">
        <span className={`cadence-picker-preview-text${validationError ? ' is-error' : ''}`}>
          {previewLabel}
        </span>
        {cadenceString && !validationError && (
          <span className="cadence-picker-preview-string">→ {cadenceString}</span>
        )}
      </div>

      {!embedded && (
        <div className="cadence-picker-actions">
          <button type="button" className="cadence-picker-btn is-cancel" onClick={onCancel}>
            cancel
          </button>
          <button
            type="button"
            className="cadence-picker-btn is-save"
            onClick={handleSave}
            disabled={!!validationError || !cadenceString}
          >
            save
          </button>
        </div>
      )}
    </div>
  );
}

