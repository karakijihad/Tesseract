// AU-7 S1 — ScheduledNextPane.
//
// Shows enabled jobs from the existing scheduler. The scheduler does
// not expose a precise next_run_at, so this pane orders by name and
// surfaces cadence + last fired time. Lets the operator see what
// background work is queued without leaving the dashboard.

import React from 'react';
import type { ScheduleJob } from '../../lib/types';

interface ScheduledNextPaneProps {
  jobs: ScheduleJob[];
}

function _fmtTimeAgo(iso: string | null): string {
  if (!iso) return 'never';
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return 'never';
  const seconds = Math.max(0, (Date.now() - parsed) / 1000);
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 90000) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

export function ScheduledNextPane({ jobs }: ScheduledNextPaneProps): React.ReactElement {
  const enabled = jobs
    .filter((j) => j.runtime?.enabled ?? j.enabled)
    .sort((a, b) => a.name.localeCompare(b.name));

  return (
    <section className="runtime-block autonomy-pane autonomy-pane--scheduled">
      <div className="runtime-block__title">
        Scheduled
        <span className="t-meta" style={{ marginLeft: 8 }}>{enabled.length} enabled</span>
      </div>

      {enabled.length === 0 ? (
        <p className="t-meta">No scheduled jobs enabled.</p>
      ) : (
        <ul className="autonomy-list">
          {enabled.slice(0, 8).map((job) => {
            const runtime = job.runtime;
            const broken = runtime?.circuit_broken ?? false;
            return (
              <li
                key={job.name}
                className={`autonomy-row${broken ? ' autonomy-row--broken' : ''}`}
              >
                <div className="autonomy-row__head">
                  <span className={`autonomy-chip autonomy-chip--${broken ? 'broken' : 'ok'}`}>
                    {broken ? 'broken' : 'ok'}
                  </span>
                  <span className="autonomy-chip autonomy-chip--source">{job.cadence}</span>
                </div>
                <div className="autonomy-row__goal">{job.name}</div>
                <div className="autonomy-row__rationale t-meta">
                  last fired {_fmtTimeAgo(runtime?.last_fired_at ?? null)}
                  {runtime?.consecutive_failures
                    ? ` · ${runtime.consecutive_failures} consec failures`
                    : ''}
                </div>
              </li>
            );
          })}
          {enabled.length > 8 && (
            <li className="t-meta">…{enabled.length - 8} more (see Schedule tab)</li>
          )}
        </ul>
      )}
    </section>
  );
}
