import { useEffect, useState } from 'react';

import { Hint } from '../../components/ui/Hint';
import { useAlarmsStore } from '../../stores/alarms';
import type { Alarm } from '../../lib/types';
import { AddAlarmForm } from './AddAlarmForm';

const TICK_MS = 1000;

function firesIn(runAtIso: string, now: number): string {
  const ms = new Date(runAtIso).getTime() - now;
  if (Number.isNaN(ms)) return '—';
  if (ms <= 0) return 'now';
  const secs = Math.floor(ms / 1000);
  if (secs < 60) return `${secs}s`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m${String(secs % 60).padStart(2, '0')}s`;
  const hrs = Math.floor(secs / 3600);
  const rem = secs - hrs * 3600;
  return `${hrs}h${String(Math.floor(rem / 60)).padStart(2, '0')}m`;
}

function recurrenceLabel(alarm: Alarm): string | null {
  const rec = alarm.recurrence;
  if (!rec) return null;
  if (rec.kind === 'every' && rec.interval_seconds) {
    const s = rec.interval_seconds;
    if (s % 3600 === 0) return `every ${s / 3600}h`;
    if (s % 60 === 0) return `every ${s / 60}m`;
    return `every ${s}s`;
  }
  if (rec.kind === 'weekly') {
    const names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    return rec.weekday !== undefined ? `weekly ${names[rec.weekday] ?? '?'}` : 'weekly';
  }
  return rec.kind;
}

interface RowProps {
  alarm: Alarm;
  now: number;
  onCancel: (handle: string) => void;
  onSnooze: (handle: string) => void;
}

function AlarmRow({ alarm, now, onCancel, onSnooze }: RowProps) {
  const rec = recurrenceLabel(alarm);
  return (
    <div className="schedule-row">
      <div className="schedule-row-head">
        <span className="schedule-name">{alarm.label}</span>
        {rec && <span className="schedule-cadence-pill">{rec}</span>}
        <span className="t-meta" style={{ marginLeft: 'auto' }}>
          fires in {firesIn(alarm.run_at, now)}
        </span>
        <Hint label="Snooze 10 minutes" position="bottom">
          <button
            type="button"
            className="schedule-header-btn"
            onClick={() => onSnooze(alarm.label)}
          >
            snooze
          </button>
        </Hint>
        <Hint label="Cancel this alarm" position="bottom">
          <button
            type="button"
            className="schedule-remove-btn"
            onClick={() => onCancel(alarm.label)}
          >
            ×
          </button>
        </Hint>
      </div>
      {alarm.message && (
        <div className="schedule-row-meta">
          <span className="t-meta">{alarm.message}</span>
        </div>
      )}
    </div>
  );
}

export function AlarmsPanel() {
  const alarms = useAlarmsStore((s) => s.alarms);
  const loading = useAlarmsStore((s) => s.loading);
  const lastError = useAlarmsStore((s) => s.lastError);
  const fetchAlarmsFn = useAlarmsStore((s) => s.fetchAlarms);
  const cancel = useAlarmsStore((s) => s.cancel);
  const snooze = useAlarmsStore((s) => s.snooze);
  const [now, setNow] = useState(() => Date.now());
  const [adding, setAdding] = useState(false);

  useEffect(() => {
    fetchAlarmsFn();
  }, [fetchAlarmsFn]);

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(id);
  }, []);

  return (
    <div className="schedule-view">
      <div className="schedule-header">
        <span className="schedule-header-title">Alarms</span>
        <span className="schedule-header-meta">{alarms.length} pending</span>
        <div className="schedule-header-actions">
          <Hint label="Add a new alarm" position="bottom">
            <button
              type="button"
              onClick={() => setAdding((v) => !v)}
              className="schedule-header-btn"
            >
              {adding ? '× cancel' : '+ add'}
            </button>
          </Hint>
          <Hint label="Re-fetch pending alarms" position="bottom">
            <button
              type="button"
              onClick={() => fetchAlarmsFn()}
              className="schedule-header-btn"
            >
              refresh
            </button>
          </Hint>
        </div>
      </div>
      {adding && <AddAlarmForm onClose={() => setAdding(false)} />}
      {lastError && <div className="schedule-error">{lastError}</div>}
      <div className="schedule-list">
        {alarms.length === 0 && !loading && (
          <div className="schedule-empty">
            <span className="t-meta">No pending alarms — click + add, /alarm_set, or just ask.</span>
          </div>
        )}
        {alarms.map((alarm) => (
          <AlarmRow
            key={alarm.id}
            alarm={alarm}
            now={now}
            onCancel={(h) => cancel(h)}
            onSnooze={(h) => snooze(h, '10m')}
          />
        ))}
      </div>
    </div>
  );
}
