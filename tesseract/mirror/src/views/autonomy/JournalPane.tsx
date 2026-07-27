// TC-1 — JournalPane: reverse-chronological operator journal feed.

import React from 'react';
import type { OperatorJournalRow } from '../../lib/api';

interface JournalPaneProps {
  rows: OperatorJournalRow[];
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
}

const EVENT_LABEL: Record<string, string> = {
  approval: 'approved',
  dispatch: 'dispatched',
  outcome: 'outcome',
  advice_only: 'advice only',
  follow_up_draft: 'follow-up',
};

function _formatTs(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function _renderDetail(row: OperatorJournalRow): React.ReactNode {
  if (row.summary) return row.summary;
  if (row.event_type === 'dispatch' && row.worker_id) return `worker ${row.worker_id}`;
  if (row.event_type === 'outcome' && row.worker_id) {
    const status = typeof row.status === 'string' ? row.status : 'terminal';
    return `worker ${row.worker_id} → ${status}`;
  }
  return row.agenda_item_id ?? '—';
}

export function JournalPane({ rows, status, error }: JournalPaneProps): React.ReactElement {
  if (status === 'error') {
    return (
      <section
        className="runtime-block autonomy-pane autonomy-pane--journal"
        data-testid="autonomy-journal-pane"
      >
        <div className="runtime-block__title">Operator journal</div>
        <p className="t-meta">Failed to load: {error}</p>
      </section>
    );
  }

  return (
    <section
      className="runtime-block autonomy-pane autonomy-pane--journal"
      data-testid="autonomy-journal-pane"
    >
      <div className="runtime-block__title">
        Operator journal
        <span className="t-meta" style={{ marginLeft: 8 }}>{rows.length}</span>
      </div>

      {rows.length === 0 ? (
        <p className="t-meta">
          No journal entries yet. Approvals, dispatches, and worker
          outcomes will land here once the autonomy kernel runs.
        </p>
      ) : (
        <ul className="autonomy-list autonomy-journal" data-testid="autonomy-journal-list">
          {rows.map((row, idx) => {
            const label = EVENT_LABEL[row.event_type] ?? row.event_type;
            const kindClass = `autonomy-chip autonomy-chip--journal-${row.event_type}`;
            const key = `${row.ts}-${row.event_type}-${row.agenda_item_id ?? row.worker_id ?? idx}`;
            return (
              <li
                key={key}
                className={`autonomy-row autonomy-row--journal autonomy-row--journal-${row.event_type}`}
                data-event-type={row.event_type}
              >
                <div className="autonomy-row__head">
                  <span className={kindClass}>{label}</span>
                  <span className="t-meta">{_formatTs(row.ts)}</span>
                </div>
                <div className="autonomy-row__goal">{_renderDetail(row)}</div>
                {(row.agenda_item_id || row.worker_id) && (
                  <div className="t-meta">
                    {row.agenda_item_id && <span>agenda {row.agenda_item_id}</span>}
                    {row.agenda_item_id && row.worker_id && <span> · </span>}
                    {row.worker_id && <span>worker {row.worker_id}</span>}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
