import { useEffect, useRef, useState } from 'react';

import { BACKEND_BASE } from '../../lib/endpoints';
import type { RecoverySummaryPayload } from '../autonomy/RecoveryPane';
import { Block } from '../../components/common/Block';
import { Button } from '../../components/common/Button';
import { Hint } from '../../components/ui/Hint';
import { useAutonomyStore } from '../../stores/autonomy';
import { Note } from '../../components/common/Note';

// AU-1 — supervisor visibility panel. Polls /api/runtime/status every
// 5 seconds. Reports supervisor alive / pid, backend uptime, last
// shutdown intent observed, and any crash-storm marker.
//
// The data comes from supervisor-owned files on disk
// (<TESSERACT_HOME>/runtime/{intent.json, crash_storm.json,
// supervisor.pid}); the backend just reads them. Supervisor never
// talks to Mirror — file-based status only.
//
// Back in Settings, under About (operator, 2026-08-13): uptime, PIDs and the
// runtime directory answer "what is this install", which is what About is,
// and they were never about the agenda they sat beside. The embedded
// RecoveryPane stays dropped — the Autonomy side column renders one from the
// WS-driven autonomy store, so "Last recovery" is not duplicated.

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
const SHUTDOWN_KEY = 'runtime:shutdown';
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

  // Restart-needed and shutdown are runtime facts, so they live with the
  // runtime rather than in the Autonomy header, where they read as something
  // the agenda had done (operator, 2026-08-14).
  const runtimeShutdown = useAutonomyStore((st) => st.runtimeShutdown);
  const pendingActions = useAutonomyStore((st) => st.pendingActions);
  const shutdownBusy = pendingActions.has(SHUTDOWN_KEY);
  const [shutdownArmed, setShutdownArmed] = useState(false);

  // Confirm-then-fire: the first click arms, a second within 6s calls
  // /api/runtime/shutdown.
  useEffect(() => {
    if (!shutdownArmed) return;
    const t = setTimeout(() => setShutdownArmed(false), 6000);
    return () => clearTimeout(t);
  }, [shutdownArmed]);

  const onShutdownClick = () => {
    if (shutdownBusy) return;
    if (!shutdownArmed) {
      setShutdownArmed(true);
      return;
    }
    setShutdownArmed(false);
    void runtimeShutdown();
  };

  let shutdownLabel = 'shutdown';
  if (shutdownBusy) shutdownLabel = 'shutting down…';
  else if (shutdownArmed) shutdownLabel = 'confirm shutdown';

  return (
    <Block
      title="Runtime"
      meta={
        <>
          <span className={`runtime-pill runtime-pill--${label.tone}`}>{label.text}</span>
          <Hint label="Operator-initiated clean shutdown (operator_quit intent — the supervisor will not respawn)">
            <Button
              tone="danger"
              active={shutdownArmed}
              onClick={onShutdownClick}
              disabled={shutdownBusy}
              ariaLabel="shutdown backend"
              testId="runtime-shutdown"
            >
              {shutdownLabel}
            </Button>
          </Hint>
        </>
      }
    >
      <Note>
        The supervisor respawns Mirror on crash, refuses to respawn after
        operator_quit, and latches after 3 crashes in 5 minutes. Launch with{' '}
        <code>tesseract-start.bat</code> (Windows) or{' '}
        <code>python -m tesseract.supervisor</code>. Status polls every{' '}
        {POLL_INTERVAL_MS / 1000}s.
      </Note>

      <dl className="runtime-kv">
        <dt>Backend uptime</dt>
        <dd>{status ? fmtUptime(status.backend.uptime_seconds) : '—'}</dd>
        <dt>Backend pid</dt>
        <dd>{status?.backend.pid ?? '—'}</dd>
        <dt>Supervisor pid</dt>
        <dd>{status?.supervisor.pid ?? '—'}</dd>
        <dt>Runtime dir</dt>
        {/* No `t-meta` here. The tier dropped this one value to 9px while its
            three siblings rendered at 12.6px, so a four-row list read as two
            different kinds of fact. The path only needs to wrap. */}
        <dd className="runtime-kv__path">{status?.runtime_dir ?? '—'}</dd>
      </dl>

      {status?.intent && (
        <Block
          title="Last persisted intent"
          titleHint={
            'Why the backend stopped LAST time, written by whoever stopped it and read by ' +
            'the supervisor on the next launch. `operator_quit` means you asked it to stop, ' +
            'so the supervisor deliberately did not respawn it; anything else is a stop the ' +
            'supervisor treats as a crash and recovers from. This is history, not the ' +
            'current state — a stale record beside a live backend is normal.'
          }
        >
          <pre className="runtime-block__pre t-meta">
            {JSON.stringify(status.intent, null, 2)}
          </pre>
        </Block>
      )}

      {status?.crash_storm && (
        <Block title="Crash storm latched" tone="bad">
          <p className="t-meta">{status.crash_storm.reason}</p>
          <Note tone="warn">
            Clear with <code>tesseract\scripts\clear_crash_storm.bat</code> or{' '}
            <code>python -m tesseract.scripts.clear_crash_storm</code>.
          </Note>
        </Block>
      )}

      {error && <Note tone="bad">runtime status: {error}</Note>}
    </Block>
  );
}
