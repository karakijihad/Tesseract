import { useEffect, useMemo, useState } from 'react';

import { Segmented } from '../../components/common/Segmented';
import { Button } from '../../components/common/Button';
import { Input } from '../../components/common/Input';
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
  parseCadence,
  validateDaily,
  validateInterval,
} from './cadence';
import { readCadence } from '../../lib/api';
import type { CadenceReading } from '../../lib/types';

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
  interval: 'Every so often. It fires the given gap after its last run, which suits heartbeats and polling.',
  daily: 'Once per day at a fixed wall-clock time. Good for nightly rollups and morning digests.',
  cron: 'Full 5-field cron expression (min hour day-of-month month day-of-week). For anything irregular: weekdays only, every 15 min, 1st of month, etc.',
};

/** Long enough that typing a cron field does not ask on every keystroke. */
const READ_DEBOUNCE_MS = 250;

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
  // The picker's own fields are its own business: how many minutes is a
  // question about this form. Whether the SCHEDULER will take the string they
  // make is not, and answering it here is what put four cadences out of reach
  // that the runtime would have run.
  const { cadenceString, localError } = useMemo(() => {
    if (mode === 'interval') {
      const err = validateInterval(interval);
      return { cadenceString: err ? '' : formatInterval(interval), localError: err };
    }
    if (mode === 'daily') {
      const err = validateDaily(daily);
      return { cadenceString: err ? '' : formatDaily(daily), localError: err };
    }
    return { cadenceString: formatCron(cron), localError: null as string | null };
  }, [mode, interval, daily, cron]);

  const [reading, setReading] = useState<CadenceReading | null>(null);
  const [reachedIt, setReachedIt] = useState(true);

  useEffect(() => {
    if (localError || !cadenceString) {
      setReading(null);
      return;
    }
    let live = true;
    const timer = window.setTimeout(() => {
      readCadence(cadenceString)
        .then((r) => {
          if (!live) return;
          setReading(r);
          setReachedIt(true);
        })
        .catch(() => {
          if (!live) return;
          setReading(null);
          setReachedIt(false);
        });
    }, READ_DEBOUNCE_MS);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
  }, [cadenceString, localError]);

  const validationError = localError ?? (reading && !reading.ok ? reading.problem : null);
  // Saving is allowed while the answer is in flight and when the backend could
  // not be reached: the scheduler refuses a cadence it will not take, with its
  // own sentence, and a form that blocks on its own uncertainty is the thing
  // this replaced.
  const settled = !localError && !!cadenceString && (reading === null ? !reachedIt : reading.ok);

  const previewLabel = useMemo(() => {
    if (localError) return localError;
    if (!cadenceString) return 'enter a cadence';
    if (!reachedIt) return 'the app could not be reached, so this is unchecked';
    if (!reading) return 'reading it';
    if (!reading.ok) return reading.problem;
    if (!reading.nextFireAt) return reading.words;
    const next = new Date(reading.nextFireAt);
    return `${reading.words}, next in ${humanizeDelta(next.getTime() - Date.now())}`;
  }, [cadenceString, localError, reading, reachedIt]);

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
    if (embedded) onCommit(settled ? cadenceString : '');
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cadenceString, settled, embedded]);

  const handleSave = () => {
    if (!settled) return;
    onCommit(cadenceString);
  };

  return (
    <div className="cadence-picker" role="group" aria-label={`Edit cadence for ${jobName}`}>
      <Segmented
        items={(Object.keys(MODE_LABELS) as CadenceMode[]).map((m) => ({
          key: m,
          label: MODE_LABELS[m],
          hint: MODE_HINTS[m],
        }))}
        value={mode}
        onSelect={setMode}
        label="Cadence shape"
      />

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
              <Input
                className="cadence-picker-cron-input"
                value={cron[key]}
                onChange={(next) => setCron({ ...cron, [key]: next })}
                placeholder={CRON_PLACEHOLDERS[key]}
                ariaLabel={CRON_PLACEHOLDERS[key]}
                spellCheck={false}
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
          <Button onClick={onCancel}>cancel</Button>
          <Button
            tone="primary"
            onClick={handleSave}
            disabled={!settled}
          >
            save
          </Button>
        </div>
      )}
    </div>
  );
}

