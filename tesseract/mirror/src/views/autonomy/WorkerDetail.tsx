// One worker, opened a level down inside the pane.
//
// The same card shape every level 3 on this panel uses: what it was asked to
// do, a grid of facts, then bands of state lines. It drew a modal head, four
// chips and a key-value grid of its own until 2026-08-25.
//
// The only words this file authors are field names. What the worker was asked,
// what it reported and why it stopped are its own record, rendered as written.

import React from 'react';
import { useAutonomyStore } from '../../stores/autonomy';
import type { WorkerDetail as WorkerRecord } from '../../lib/api';
import { Fact } from '../../components/common/Fact';
import { Note } from '../../components/common/Note';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
import { clock } from '../../lib/time';

// A transition into one of these means the worker did not do its job.
const BADLY = new Set(['failed', 'tool_error', 'interrupted', 'cancelled']);

function howLong(seconds: number | null | undefined): string {
  if (!seconds || seconds < 1) return 'no time at all';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

/** What it cost, in the terms its own billing uses. A subscription seat has no
 *  per-call usage and saying `$0.0000` would read as free rather than as
 *  already paid for. */
function whatItCost(w: WorkerRecord): string {
  if (w.billing === 'subscription') return 'a flat rate plan, nothing per call';
  const total = w.tokens_in + w.tokens_out;
  const cost = w.cost_usd > 0 ? `$${w.cost_usd.toFixed(4)}` : 'nothing recorded';
  return `${total} tokens, ${cost}`;
}

export function WorkerDetail(): React.ReactElement | null {
  const selectedId = useAutonomyStore((s) => s.selectedWorkerId);
  const worker = useAutonomyStore((s) => s.workerDetail);
  const status = useAutonomyStore((s) => s.workerDetailStatus);
  const error = useAutonomyStore((s) => s.workerDetailError);

  if (selectedId == null) return null;
  if (status === 'error') {
    return <Note tone="bad">This worker could not be read. {error}</Note>;
  }
  if (!worker) return <></>;

  const timeline: StateLine[] = worker.status_history
    .slice()
    .reverse()
    .map((t, i) => ({
      key: `${t.at}:${i}`,
      state: BADLY.has(t.to_status) ? 'degraded' : 'idle',
      label: t.to_status.replace(/_/g, ' '),
      name: t.from_status || 'started',
      said: t.reason || '',
      when: clock(t.at),
      value: t.to_status.replace(/_/g, ' '),
    }));

  return (
    <div className="entry-card" data-testid="worker-detail">
      <p className="entry-card__summary">{worker.prompt || 'It was given no prompt.'}</p>

      {worker.summary && (
        <>
          <Band label="What it reported" />
          <div className="entry-card__why">{worker.summary}</div>
        </>
      )}

      {(worker.error_class || worker.error_message) && (
        <Note tone="bad">
          {worker.error_class ? `${worker.error_class}. ` : ''}
          {worker.error_message}
        </Note>
      )}

      <Band label="What it is" />
      <div className="entry-card__facts">
        <Fact label="Doing">{worker.status.replace(/_/g, ' ')}</Fact>
        <Fact label="Kind">{worker.kind}</Fact>
        {worker.role && <Fact label="Role">{worker.role}</Fact>}
        <Fact label="Risk">{worker.risk_class.replace(/_/g, ' ')}</Fact>
        <Fact label="Took">{howLong(worker.duration_seconds)}</Fact>
        <Fact label="Cost">{whatItCost(worker)}</Fact>
        <Fact label="Started">{clock(worker.created_at)}</Fact>
        <Fact label="Last said anything">{clock(worker.updated_at)}</Fact>
        {worker.retry_count > 0 && <Fact label="Tried again">{worker.retry_count}</Fact>}
        {worker.exit_code != null && <Fact label="Exit">{worker.exit_code}</Fact>}
      </div>

      <Band label="Where to find it" />
      <div className="entry-card__facts">
        <Fact label="For">{worker.agenda_item_id}</Fact>
        {worker.parent_worker_id && (
          <Fact label="Spawned by">{worker.parent_worker_id}</Fact>
        )}
        {worker.pid != null && <Fact label="Process">{worker.pid}</Fact>}
        {worker.pane_id && <Fact label="Pane">{worker.pane_id}</Fact>}
        {worker.worktree_path && <Fact label="Worktree">{worker.worktree_path}</Fact>}
        {worker.transcript_path && (
          <Fact label="Transcript">{worker.transcript_path}</Fact>
        )}
        {worker.cli_invocation && worker.cli_invocation.length > 0 && (
          <Fact label="Run as">{worker.cli_invocation.join(' ')}</Fact>
        )}
      </div>

      {worker.artifacts.length > 0 && (
        <div className="autonomy-group">
          <Band label="What it left behind" count={worker.artifacts.length} />
          <StateStrip
            lines={worker.artifacts.map((a, i) => ({
              key: `${a.path}:${i}`,
              state: 'idle' as const,
              label: a.kind,
              name: a.path,
              said: a.kind,
              value: a.size_bytes != null ? `${a.size_bytes} bytes` : '',
            }))}
          />
        </div>
      )}

      {timeline.length > 0 && (
        <div className="autonomy-group">
          <Band label="How it got here" count={timeline.length} />
          <StateStrip lines={timeline} />
        </div>
      )}
    </div>
  );
}
