// AU-2 — minimal Recovery pane.
//
// Renders the last RecoveryManager pass: boot id, per-scan counts,
// operator_attention list. Pulls data
// from `runtimeStatus.last_recovery` so the caller is just a polled
// snapshot — no separate fetch from this component.
//
// AU-7 (the full Autonomy Dashboard) embeds this pane in its parent
// shell. For S2 it's surfaced inside the Settings Runtime section so
// the operator can see recovery state today without waiting for AU-7.

import { Block } from '../../components/common/Block';
import React from 'react';

export interface RecoveryScans {
  [scan: string]: { [bucket: string]: number };
}

export interface RecoveryAttention {
  kind: string;
  id: string;
  reason: string;
}

export interface RecoverySummaryPayload {
  boot_id: string | null;
  downtime_seconds: number;
  scans: RecoveryScans;
  operator_attention: RecoveryAttention[];
  started_at: string | null;
}

export interface RecoveryPaneProps {
  summary: RecoverySummaryPayload | null;
  recoveryState: 'recovering' | 'ready';
}


function bucketCells(scans: RecoveryScans, scan: string): { label: string; count: number }[] {
  const block = scans[scan] || {};
  return Object.entries(block).map(([label, count]) => ({ label, count }));
}


function nonEmpty(scans: RecoveryScans, scan: string): boolean {
  return bucketCells(scans, scan).some((c) => c.count > 0);
}


function fmtTimeAgo(iso: string | null): string {
  if (!iso) return '—';
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return '—';
  const seconds = Math.max(0, (Date.now() - parsed) / 1000);
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 90000) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}


export function RecoveryPane({ summary, recoveryState }: RecoveryPaneProps): React.ReactElement {
  if (recoveryState === 'recovering') {
    return (
      <Block title="Recovery in progress" tone="warn">
        <p className="t-meta">RecoveryManager is reconciling boot-time state. Heartbeat suspended; /api/health returns 503 until ready.</p>
      </Block>
    );
  }
  if (!summary || !summary.boot_id) {
    return (
      <Block title="Recovery">
        <p className="t-meta">No recovery pass recorded in this backend lifetime yet.</p>
      </Block>
    );
  }

  const attn = summary.operator_attention;
  const hasSchedule = nonEmpty(summary.scans, 'schedule');

  return (
    <Block title="Last recovery" meta={<>boot {summary.boot_id.slice(-9)} · {fmtTimeAgo(summary.started_at)}</>} tone={attn.length ? "warn" : "default"}>

      {hasSchedule && (
        <dl className="recovery-scans">
          <dt>Schedule (24h)</dt>
          <dd>
            {bucketCells(summary.scans, 'schedule')
              .filter((c) => c.count > 0)
              .map((c) => `${c.count} ${c.label}`)
              .join(' · ')}
          </dd>
        </dl>
      )}

      {attn.length > 0 && (
        <div className="recovery-attention">
          <div className="recovery-attention__title">
            Operator attention ({attn.length})
          </div>
          <ul className="recovery-attention__list">
            {attn.slice(0, 6).map((a, i) => (
              <li key={`${a.kind}-${a.id}-${i}`}>
                <span className="recovery-attention__kind">{a.kind}</span>
                <code>{a.id}</code>
                <span className="t-meta">{a.reason}</span>
              </li>
            ))}
            {attn.length > 6 && (
              <li className="t-meta">…{attn.length - 6} more (see recovery_summary event in workspace)</li>
            )}
          </ul>
        </div>
      )}

      {!attn.length && (
        <p className="t-meta">Clean boot — no in-flight state required operator attention.</p>
      )}
    </Block>
  );
}
