// AU-7 S1 — WorkersPane.
//
// Live + recently-transitioned workers under workers/active/. Active
// statuses are listed first (running, awaiting_io, spawning, queued),
// then recent terminals. Answers §15 Q1 (what is the assistant doing) by
// showing the actual process surface, not just the agenda intent.

import React from 'react';
import type { ActiveWorker } from '../../lib/api';
import { useAutonomyStore } from '../../stores/autonomy';

interface WorkersPaneProps {
  workers: ActiveWorker[];
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
}

const LIVE_STATUSES = new Set([
  'queued',
  'spawning',
  'running',
  'awaiting_io',
]);

function _shortId(id: string): string {
  return id.length > 18 ? `${id.slice(0, 8)}…${id.slice(-6)}` : id;
}

// Phase 7 (2026-05-22) — distinguish wall-clock timeouts from real tool
// failures. Worker records all surface as `status=failed reason=tool_error`,
// but the summary/error_message text reveals which is which. Operators
// were misreading 5-minute heartbeat timeouts as broken tools.
function _failureBadge(worker: ActiveWorker): string {
  if (worker.status !== 'failed') return '';
  const blob = `${worker.summary ?? ''} ${worker.last_transition?.reason ?? ''}`.toLowerCase();
  if (blob.includes('timed out') || blob.includes('timeout')) return 'TIMEOUT';
  return 'TOOL_ERROR';
}

function _fmtDuration(seconds: number): string {
  if (!seconds || seconds < 1) return '';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

function _fmtCost(usd: number): string {
  if (usd <= 0) return '—';
  if (usd < 0.01) return `<$0.01`;
  return `$${usd.toFixed(2)}`;
}

function _fmtTokens(record: ActiveWorker): string {
  const total = record.tokens_in + record.tokens_out;
  if (total === 0) return '—';
  if (total >= 10_000) return `${(total / 1000).toFixed(1)}k tok`;
  return `${total} tok`;
}

// For subscription-billed workers (claude_cli / codex_cli) the CLI
// returns no per-call usage — $0/0 tok is not a missing value, it's the
// accurate truth (flat-rate plan). Show a "sub" chip instead so the
// operator doesn't read $0 as "tool free / something broken".
function _usageCell(record: ActiveWorker): string {
  if (record.billing === 'subscription') return 'sub';
  return `${_fmtTokens(record)} · ${_fmtCost(record.cost_usd)}`;
}

export function WorkersPane({ workers, status, error }: WorkersPaneProps): React.ReactElement {
  const openWorkerDetail = useAutonomyStore((s) => s.openWorkerDetail);
  if (status === 'error') {
    return (
      <section className="runtime-block autonomy-pane autonomy-pane--workers">
        <div className="runtime-block__title">Workers</div>
        <p className="t-meta">Failed to load: {error}</p>
      </section>
    );
  }

  const live = workers.filter((w) => LIVE_STATUSES.has(w.status));
  const recent = workers
    .filter((w) => !LIVE_STATUSES.has(w.status))
    .slice(0, 5);

  if (status !== 'ready' && workers.length === 0) {
    return (
      <section className="runtime-block autonomy-pane autonomy-pane--workers">
        <div className="runtime-block__title">Workers</div>
        <p className="t-meta">Loading…</p>
      </section>
    );
  }

  return (
    <section className="runtime-block autonomy-pane autonomy-pane--workers">
      <div className="runtime-block__title">
        Workers
        <span className="t-meta" style={{ marginLeft: 8 }}>{live.length} live · {recent.length} recent</span>
      </div>

      {workers.length === 0 ? (
        <p className="t-meta">No workers active. Records appear here as the kernel dispatches.</p>
      ) : (
        <ul className="autonomy-list">
          {live.map((w) => (
            <li
              key={w.id}
              className={`autonomy-row autonomy-row--${w.status} autonomy-row--clickable`}
              onClick={() => void openWorkerDetail(w.id)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  void openWorkerDetail(w.id);
                }
              }}
            >
              <div className="autonomy-row__head">
                <span className={`autonomy-chip autonomy-chip--${w.status}`}>{w.status}</span>
                <span className="autonomy-chip autonomy-chip--kind">{w.kind}</span>
                <span className="autonomy-chip autonomy-chip--source">{w.role}</span>
                <span className="autonomy-row__score">{_usageCell(w)}</span>
              </div>
              <div className="autonomy-row__goal t-mono">{_shortId(w.id)}</div>
              {w.summary && <div className="autonomy-row__rationale t-meta">{w.summary}</div>}
            </li>
          ))}
          {recent.length > 0 && (
            <li className="autonomy-list__divider t-meta">recent terminals</li>
          )}
          {recent.map((w) => {
            const failureBadge = _failureBadge(w);
            const duration = _fmtDuration(w.duration_seconds ?? 0);
            return (
              <li
                key={w.id}
                className={`autonomy-row autonomy-row--${w.status} autonomy-row--clickable`}
                onClick={() => void openWorkerDetail(w.id)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    void openWorkerDetail(w.id);
                  }
                }}
              >
                <div className="autonomy-row__head">
                  <span className={`autonomy-chip autonomy-chip--${w.status}`}>{w.status}</span>
                  <span className="autonomy-chip autonomy-chip--kind">{w.kind}</span>
                  <span className="autonomy-chip autonomy-chip--source">{w.role}</span>
                  {failureBadge && (
                    <span
                      className={`autonomy-chip autonomy-chip--${failureBadge === 'TIMEOUT' ? 'awaiting_io' : 'failed'}`}
                      title={
                        failureBadge === 'TIMEOUT'
                          ? 'wall-clock timeout — worker was still working when its budget ran out'
                          : 'tool returned an error or denied path'
                      }
                    >
                      {failureBadge}
                    </span>
                  )}
                  <span className="autonomy-row__score">
                    {duration && <>{duration} · </>}
                    {_usageCell(w)}
                  </span>
                </div>
                <div className="autonomy-row__goal t-mono">{_shortId(w.id)}</div>
                {w.last_transition?.reason && (
                  <div className="autonomy-row__rationale t-meta">{w.last_transition.reason}</div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
