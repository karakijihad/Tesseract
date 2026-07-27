import { useState } from 'react';
import { sendCommand } from '../../lib/commands';
import { stopAlarmToneFor } from '../../lib/alarmSound';
import { BACKEND_BASE } from '../../lib/endpoints';
import { useToastStore, type Toast } from '../../stores/toasts';
import { useWebSocketStore } from '../../stores/websocket';

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
        if (t.restart) {
          return <RestartToast key={t.id} toast={t} onClose={() => dismiss(t.id)} />;
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

function RestartToast({ toast, onClose }: { toast: Toast; onClose: () => void }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const meta = toast.restart!;

  const restart = async () => {
    setPending(true);
    setError(null);
    try {
      const sessionId = useWebSocketStore.getState().sessionId;
      if (!sessionId) {
        setError('no active session');
        setPending(false);
        return;
      }
      const resp = await fetch(`${BACKEND_BASE}/api/runtime/restart_for_code_drift`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          session_id: sessionId,
          head_sha: meta.headSha,
          reason: `operator clicked restart toast (${meta.pathCount} backend path(s) changed)`,
        }),
      });
      if (!resp.ok) {
        setError(`HTTP ${resp.status}`);
        setPending(false);
        return;
      }
      // Backend will stop the event loop after a 500ms grace; the supervisor
      // respawns. The WS will disconnect — leave the toast sticky so the
      // operator sees the action took effect; existing reconnect logic
      // re-establishes the session once the new backend binds.
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPending(false);
    }
  };

  return (
    <div className={`toast toast--${toast.kind} toast--restart`} role="status">
      <div className="toast-body">{toast.message}</div>
      <div className="toast-actions">
        <button
          type="button"
          className="toast-action"
          onClick={restart}
          disabled={pending}
        >
          {pending ? 'Restarting…' : 'Restart now'}
        </button>
        <button type="button" className="toast-action" onClick={onClose} disabled={pending}>
          Later
        </button>
      </div>
      {error && <div className="toast-error t-meta">{error}</div>}
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
          <button
            type="button"
            className="toast-action"
            onClick={() => setPickerOpen((v) => !v)}
            aria-haspopup="menu"
            aria-expanded={pickerOpen}
          >
            Snooze ▾
          </button>
          {pickerOpen && (
            <div className="toast-snooze-menu" role="menu">
              {alarm.snoozeOptions.map((opt) => (
                <button
                  key={opt}
                  type="button"
                  className="toast-snooze-opt"
                  onClick={() => snooze(opt)}
                  role="menuitem"
                >
                  {opt}
                </button>
              ))}
            </div>
          )}
        </div>
        <button type="button" className="toast-action" onClick={dismissAlarm}>
          Dismiss
        </button>
      </div>
    </div>
  );
}
