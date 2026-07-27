import { useEffect, useRef, useState } from 'react';

import { BACKEND_BASE } from '../../lib/endpoints';
import type { RecoverySummaryPayload } from '../autonomy/RecoveryPane';

// AU-1 — supervisor visibility panel. Polls /api/runtime/status every
// 5 seconds. Reports supervisor alive / pid, backend uptime, last
// shutdown intent observed, and any crash-storm marker.
//
// The data comes from supervisor-owned files on disk
// (<TESSERACT_HOME>/runtime/{intent.json, crash_storm.json,
// supervisor.pid}); the backend just reads them. Supervisor never
// talks to Mirror — file-based status only.
//
// 2026-05-18: relocated from Settings into the Autonomy view. The
// embedded RecoveryPane was dropped — the Autonomy side column already
// renders one from the WS-driven autonomy store, so we no longer
// duplicate "Last recovery" across two surfaces.

interface RuntimeStatus {
  supervisor: {
    pid: number | null;
    alive: boolean;
    pid_file: string;
  };
  backend: {
    uptime_seconds: number;
    pid: number;
    recovery_state?: 'recovering' | 'ready';
  };
  intent: {
    intent: string;
    timestamp: string;
    source: string;
    continuation_id?: string;
    reason?: string;
    backend_pid?: number;
  } | null;
  crash_storm: {
    latched_at: string;
    crashes: Array<{ timestamp: string; exit_code: number; last_log_tail: string }>;
    reason: string;
  } | null;
  last_recovery: RecoverySummaryPayload | null;
  runtime_dir: string;
  timestamp: string;
}

// Supervisor status panel — display-only; the UI copy below quotes this value.
const POLL_INTERVAL_MS = 30_000;


function fmtUptime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return 'unknown';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}


function supervisorLabel(s: RuntimeStatus | null): {
  text: string;
  tone: 'ok' | 'warn' | 'danger' | 'idle';
} {
  if (!s) return { text: 'querying…', tone: 'idle' };
  if (s.crash_storm) return { text: 'crash storm latched', tone: 'danger' };
  if (s.supervisor.alive) return { text: `alive (pid ${s.supervisor.pid})`, tone: 'ok' };
  if (s.supervisor.pid != null) return { text: `stale pid file (pid ${s.supervisor.pid})`, tone: 'warn' };
  return { text: 'not running — backend started without supervisor', tone: 'warn' };
}


export function RuntimeSection() {
  const [status, setStatus] = useState<RuntimeStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const resp = await fetch(`${BACKEND_BASE}/api/runtime/status`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = (await resp.json()) as RuntimeStatus;
        if (!cancelled) {
          setStatus(data);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err));
        }
      }
    };
    tick();
    timerRef.current = window.setInterval(tick, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current);
      }
    };
  }, []);

  const label = supervisorLabel(status);

  return (
    <section className="settings-section">
      <h3 className="settings-section__title">
        Runtime
        <span className={`runtime-pill runtime-pill--${label.tone}`}>{label.text}</span>
      </h3>
      <p className="t-meta">
        AU-1 supervisor: respawns Mirror on crash, refuses to respawn after operator_quit,
        latches after 3 crashes in 5 minutes. Launch with{' '}
        <code>tesseract-start.bat</code> (Windows) or{' '}
        <code>python -m tesseract.supervisor</code>. Status polls every {POLL_INTERVAL_MS / 1000}s.
      </p>

      <dl className="runtime-kv">
        <dt>Backend uptime</dt>
        <dd>{status ? fmtUptime(status.backend.uptime_seconds) : '—'}</dd>
        <dt>Backend pid</dt>
        <dd>{status?.backend.pid ?? '—'}</dd>
        <dt>Supervisor pid</dt>
        <dd>{status?.supervisor.pid ?? '—'}</dd>
        <dt>Runtime dir</dt>
        <dd className="t-meta runtime-kv__path">{status?.runtime_dir ?? '—'}</dd>
      </dl>

      {status?.intent && (
        <div className="runtime-block">
          <div className="runtime-block__title">Last persisted intent</div>
          <pre className="runtime-block__pre t-meta">
            {JSON.stringify(status.intent, null, 2)}
          </pre>
        </div>
      )}

      {status?.crash_storm && (
        <div className="runtime-block runtime-block--danger">
          <div className="runtime-block__title">Crash storm latched</div>
          <p className="t-meta">{status.crash_storm.reason}</p>
          <p className="t-meta">
            Clear with <code>tesseract\scripts\clear_crash_storm.bat</code> or{' '}
            <code>python -m tesseract.scripts.clear_crash_storm</code>.
          </p>
        </div>
      )}

      {error && <div className="settings-error">runtime status: {error}</div>}
    </section>
  );
}
