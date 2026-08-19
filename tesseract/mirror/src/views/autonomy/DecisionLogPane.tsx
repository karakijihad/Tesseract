// AU-7 S1 — DecisionLogPane.
//
// Recent kernel + governor decisions. Built from two on-hand signals:
// the latest agenda status_history entries (each transition is a
// decision the kernel made) plus the Governor's last_tick action
// counts. Answers §15 Q6 (what failed and when?).
//
// S2 will swap this for a dedicated decision-log endpoint once the
// kernel persists its per-tick records — for S1, this view is good
// enough to surface activity.

import { Block } from '../../components/common/Block';
import React from 'react';
import type { AgendaItem, GovernorTickPayload } from '../../lib/api';

interface DecisionLogPaneProps {
  items: AgendaItem[];
  lastTick: GovernorTickPayload | null;
}

function _fmtTimeAgo(iso: string): string {
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return '—';
  const seconds = Math.max(0, (Date.now() - parsed) / 1000);
  if (seconds < 90) return `${Math.round(seconds)}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 90000) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

interface DecisionRow {
  key: string;
  at: string;
  by: string;
  label: string;
  detail: string;
}

function _rows(items: AgendaItem[], lastTick: GovernorTickPayload | null): DecisionRow[] {
  const out: DecisionRow[] = [];
  for (const item of items) {
    const last = item.status_history[item.status_history.length - 1];
    if (!last) continue;
    out.push({
      key: `${item.id}-${last.at}`,
      at: last.at,
      by: last.by,
      label: `${last.from_status ?? '∅'} → ${last.to_status}`,
      detail: `${item.goal}${last.reason ? ` — ${last.reason}` : ''}`,
    });
  }
  if (lastTick && (lastTick.pauses_added.length || lastTick.workers_cancelled.length || lastTick.items_blocked.length)) {
    out.push({
      key: `governor-${lastTick.at}`,
      at: lastTick.at,
      by: 'governor',
      label: 'tick',
      detail: [
        lastTick.pauses_added.length ? `${lastTick.pauses_added.length} pause` : '',
        lastTick.workers_cancelled.length ? `${lastTick.workers_cancelled.length} cancel` : '',
        lastTick.items_blocked.length ? `${lastTick.items_blocked.length} block` : '',
      ]
        .filter(Boolean)
        .join(' · '),
    });
  }
  return out.sort((a, b) => (a.at < b.at ? 1 : -1)).slice(0, 10);
}

export function DecisionLogPane({ items, lastTick }: DecisionLogPaneProps): React.ReactElement {
  const rows = _rows(items, lastTick);

  return (
    <Block title="Recent decisions">

      {rows.length === 0 ? (
        <p className="t-meta">No decisions yet — kernel is idle.</p>
      ) : (
        <ul className="autonomy-list">
          {rows.map((row) => (
            <li key={row.key} className="autonomy-row autonomy-row--decision">
              <div className="autonomy-row__head">
                <span className="autonomy-chip autonomy-chip--source">{row.by}</span>
                <span className="autonomy-chip autonomy-chip--decision">{row.label}</span>
                <span className="t-meta autonomy-row__score">{_fmtTimeAgo(row.at)}</span>
              </div>
              <div className="autonomy-row__rationale t-meta">{row.detail}</div>
            </li>
          ))}
        </ul>
      )}
    </Block>
  );
}
