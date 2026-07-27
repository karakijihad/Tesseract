import { useEffect, useMemo, useState } from 'react';

import { Hint } from '../../components/ui/Hint';
import {
  fetchScheduleHandlers,
  postScheduleCreate,
  type ScheduleHandlerEntry,
} from '../../lib/api';
import { useToastStore } from '../../stores/toasts';
import { useScheduleStore } from '../../stores/schedule';
import { CadencePicker } from './CadencePicker';

interface Props {
  onClose: () => void;
}

const SLUG_RE = /^[a-z0-9_]+$/;

const ON_FAILURE_OPTIONS: Array<{ value: 'log' | 'alert' | 'disable'; label: string }> = [
  { value: 'log', label: 'log' },
  { value: 'alert', label: 'alert' },
  { value: 'disable', label: 'disable' },
];

export function AddJobForm({ onClose }: Props) {
  const fetchJobs = useScheduleStore((s) => s.fetchJobs);
  const [handlers, setHandlers] = useState<ScheduleHandlerEntry[]>([]);
  const [name, setName] = useState('');
  const [cadence, setCadence] = useState('1h');
  const [handler, setHandler] = useState('');
  const [enabled, setEnabled] = useState(true);
  const [onFailure, setOnFailure] = useState<'log' | 'alert' | 'disable'>('log');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchScheduleHandlers()
      .then((res) => {
        setHandlers(res.handlers);
        if (res.handlers.length > 0 && !handler) {
          setHandler(res.handlers[0].dotpath);
        }
      })
      .catch((err) =>
        setError(err instanceof Error ? err.message : String(err)),
      );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const nameValid = useMemo(() => SLUG_RE.test(name), [name]);
  const cadenceValid = cadence.trim().length > 0;
  const canSubmit = nameValid && cadenceValid && handler && !submitting;

  async function submit() {
    setSubmitting(true);
    setError(null);
    try {
      await postScheduleCreate({
        name,
        cadence,
        handler,
        enabled,
        on_failure: onFailure,
      });
      useToastStore.getState().push(`Scheduled '${name}'`);
      await fetchJobs();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="schedule-add-form" role="form" aria-label="Add scheduled job">
      <div className="schedule-add-row">
        <label className="schedule-add-label">name</label>
        <input
          type="text"
          className="schedule-add-input"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="vault_lint_4h"
          aria-invalid={name.length > 0 && !nameValid}
        />
        <Hint label="Lowercase letters, digits, underscores. Must be unique among scheduled jobs.">
          <span className="schedule-add-hint">slug</span>
        </Hint>
      </div>
      <div className="schedule-add-row schedule-add-row-picker">
        <label className="schedule-add-label">cadence</label>
        <div className="schedule-add-picker-wrap">
          <CadencePicker
            embedded
            jobName={name || 'new-job'}
            value={cadence}
            onCommit={setCadence}
            onCancel={() => {}}
          />
        </div>
      </div>
      <div className="schedule-add-row">
        <label className="schedule-add-label">handler</label>
        <select
          className="schedule-add-input"
          value={handler}
          onChange={(e) => setHandler(e.target.value)}
        >
          {handlers.map((h) => (
            <option key={h.dotpath} value={h.dotpath}>
              {h.label}
            </option>
          ))}
        </select>
      </div>
      <div className="schedule-add-row">
        <label className="schedule-add-label">on_failure</label>
        <select
          className="schedule-add-input"
          value={onFailure}
          onChange={(e) => setOnFailure(e.target.value as 'log' | 'alert' | 'disable')}
        >
          {ON_FAILURE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <label className="schedule-add-checkbox">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          enabled
        </label>
      </div>
      {error && <div className="schedule-add-error">{error}</div>}
      <div className="schedule-add-actions">
        <button
          type="button"
          className="schedule-add-cancel"
          onClick={onClose}
          disabled={submitting}
        >
          cancel
        </button>
        <button
          type="button"
          className="schedule-add-submit"
          onClick={submit}
          disabled={!canSubmit}
        >
          {submitting ? 'adding…' : 'add job'}
        </button>
      </div>
    </div>
  );
}
