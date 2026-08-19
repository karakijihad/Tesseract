import { Select } from '../../components/common/Select';
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
import { Checkbox } from '../../components/common/Checkbox';
import { Input } from '../../components/common/Input';
import { Button } from '../../components/common/Button';

interface Props {
  onClose: () => void;
}

const SLUG_RE = /^[a-z0-9_]+$/;

// The backend's floor, restated here only so the button can say no before the
// round trip. The rule itself lives in `SchedulerEngine.add_job_runtime`, which
// both creation doors go through.
const MIN_SUMMARY_CHARS = 20;

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
  const [summary, setSummary] = useState('');
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
  const summaryValid = summary.trim().length >= MIN_SUMMARY_CHARS;
  const canSubmit =
    nameValid && cadenceValid && summaryValid && handler && !submitting;

  async function submit() {
    setSubmitting(true);
    setError(null);
    try {
      await postScheduleCreate({
        name,
        cadence,
        handler,
        summary,
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
        <Input
          className="schedule-add-input"
          value={name}
          onChange={setName}
          placeholder="vault_lint_4h"
          ariaInvalid={name.length > 0 && !nameValid}
        />
        <Hint label="Lowercase letters, digits, underscores. Must be unique among scheduled jobs.">
          <span className="schedule-add-hint">slug</span>
        </Hint>
      </div>
      <div className="schedule-add-row">
        <label className="schedule-add-label">what it does</label>
        <Input
          className="schedule-add-input"
          value={summary}
          onChange={setSummary}
          placeholder="Checks the vault for broken links every four hours"
          ariaInvalid={summary.length > 0 && !summaryValid}
        />
        <Hint label="One line, for you. It is what this row says about itself in WHAT-RUNS.md and in this tab months from now.">
          <span className="schedule-add-hint">summary</span>
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
        <Select
          value={handler}
          options={handlers.map((h) => ({ value: h.dotpath, label: h.label }))}
          onChange={setHandler}
          ariaLabel="Job handler"
        />
      </div>
      <div className="schedule-add-row">
        <label className="schedule-add-label">on_failure</label>
        <Select
          value={onFailure}
          options={ON_FAILURE_OPTIONS}
          onChange={(v) => setOnFailure(v as 'log' | 'alert' | 'disable')}
          ariaLabel="On failure"
        />
        <Checkbox checked={enabled} onChange={setEnabled} label="enabled" />
      </div>
      {error && <div className="schedule-add-error">{error}</div>}
      <div className="schedule-add-actions">
        <Button
          onClick={onClose}
          disabled={submitting}
        >
          cancel
        </Button>
        <Button
          onClick={submit}
          disabled={!canSubmit}
        >
          {submitting ? 'adding…' : 'add job'}
        </Button>
      </div>
    </div>
  );
}
