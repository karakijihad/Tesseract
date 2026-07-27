import { useMemo, useState } from 'react';

import { Hint } from '../../components/ui/Hint';
import { useAlarmsStore } from '../../stores/alarms';
import { useToastStore } from '../../stores/toasts';
import { AlarmWhenPicker } from './AlarmWhenPicker';

interface Props {
  onClose: () => void;
}

const LABEL_RE = /^[A-Za-z0-9_\- ]+$/;

export function AddAlarmForm({ onClose }: Props) {
  const create = useAlarmsStore((s) => s.create);
  const [label, setLabel] = useState('');
  const [when, setWhen] = useState('30m');
  const [message, setMessage] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const labelValid = useMemo(() => LABEL_RE.test(label.trim()) && label.trim().length > 0, [label]);
  const whenValid = when.trim().length > 0;
  const canSubmit = labelValid && whenValid && !submitting;

  async function submit() {
    setSubmitting(true);
    setError(null);
    const ok = await create({
      label: label.trim(),
      when: when.trim(),
      message: message.trim() || undefined,
    });
    if (ok) {
      useToastStore.getState().push(`Alarm '${label.trim()}' set`);
      onClose();
    } else {
      // Surface the store's lastError; falls back to generic if missing.
      const lastError = useAlarmsStore.getState().lastError;
      setError(lastError ?? 'failed to create alarm');
    }
    setSubmitting(false);
  }

  return (
    <div className="schedule-add-form" role="form" aria-label="Add alarm">
      <div className="schedule-add-row">
        <label className="schedule-add-label">label</label>
        <input
          type="text"
          className="schedule-add-input"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="standup"
          aria-invalid={label.length > 0 && !labelValid}
        />
        <Hint label="Letters, digits, spaces, underscores, hyphens. Must be unique among pending alarms.">
          <span className="schedule-add-hint">name</span>
        </Hint>
      </div>
      <div className="schedule-add-row schedule-add-row-picker">
        <label className="schedule-add-label">when</label>
        <div className="schedule-add-picker-wrap">
          <AlarmWhenPicker
            embedded
            value={when}
            onCommit={setWhen}
            onCancel={() => {}}
          />
        </div>
      </div>
      <div className="schedule-add-row">
        <label className="schedule-add-label">message</label>
        <input
          type="text"
          className="schedule-add-input"
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          placeholder="optional — what to surface when it fires"
        />
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
          {submitting ? 'adding…' : 'add alarm'}
        </button>
      </div>
    </div>
  );
}
