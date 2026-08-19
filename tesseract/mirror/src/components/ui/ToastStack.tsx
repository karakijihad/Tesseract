import { useState } from 'react';
import { sendCommand } from '../../lib/commands';
import { stopAlarmToneFor } from '../../lib/alarmSound';
import { useToastStore, type Toast } from '../../stores/toasts';
import { Button } from '../common/Button';
import { MenuItem } from '../common/MenuItem';

export function ToastStack() {
  const toasts = useToastStore((s) => s.toasts);
  const dismiss = useToastStore((s) => s.dismiss);

  if (toasts.length === 0) return null;

  return (
    <div className="toast-stack" role="region" aria-live="polite" aria-label="Notifications">
      {toasts.map((t) => {
        if (t.alarm) {
          return <AlarmToast key={t.id} toast={t} onClose={() => dismiss(t.id)} />;
        }
        return (
          <button
            key={t.id}
            type="button"
            className={`toast toast--${t.kind}`}
            onClick={() => dismiss(t.id)}
          >
            {t.message}
          </button>
        );
      })}
    </div>
  );
}

function AlarmToast({ toast, onClose }: { toast: Toast; onClose: () => void }) {
  const [pickerOpen, setPickerOpen] = useState(false);
  const alarm = toast.alarm!;

  const snooze = (duration: string) => {
    sendCommand('/alarm-snooze', ` ${alarm.id} ${duration}`);
    stopAlarmToneFor(alarm.id);
    setPickerOpen(false);
    onClose();
  };

  const dismissAlarm = () => {
    sendCommand('/alarm-dismiss', ` ${alarm.id}`);
    stopAlarmToneFor(alarm.id);
    onClose();
  };

  return (
    <div className={`toast toast--${toast.kind} toast--alarm`} role="status">
      <div className="toast-body">{toast.message}</div>
      <div className="toast-actions">
        <div className="toast-snooze">
          <Button
            onClick={() => setPickerOpen((v) => !v)}
            active={pickerOpen}
            ariaExpanded={pickerOpen}
          >
            Snooze ▾
          </Button>
          {pickerOpen && (
            <div className="toast-snooze-menu" role="menu">
              {alarm.snoozeOptions.map((opt) => (
                <MenuItem key={opt} onClick={() => snooze(opt)}>
                  {opt}
                </MenuItem>
              ))}
            </div>
          )}
        </div>
        <Button onClick={dismissAlarm}>Dismiss</Button>
      </div>
    </div>
  );
}
