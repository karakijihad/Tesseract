// AU-7 — ApprovalsPane.
//
// Surfaces every agenda item in ``awaiting_operator`` plus the unfilled
// ApprovalGates per item. Answers GOVERNANCE §15 Q4 (what needs my
// approval?). S2 wires the Approve / Cancel buttons; clicking the row
// body opens the detail modal.

import React from 'react';
import type { AgendaItem } from '../../lib/api';
import { useAutonomyStore } from '../../stores/autonomy';

interface ApprovalsPaneProps {
  items: AgendaItem[];
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
}

export function ApprovalsPane({ items, status, error }: ApprovalsPaneProps): React.ReactElement {
  const approveItem = useAutonomyStore((s) => s.approveItem);
  const cancelItem = useAutonomyStore((s) => s.cancelItem);
  const openDetail = useAutonomyStore((s) => s.openDetail);
  const pending = useAutonomyStore((s) => s.pendingActions);

  if (status === 'error') {
    return (
      <section className="runtime-block autonomy-pane autonomy-pane--approvals">
        <div className="runtime-block__title">Awaiting your approval</div>
        <p className="t-meta">Failed to load: {error}</p>
      </section>
    );
  }

  const awaiting = items
    .filter((i) => i.approvals_required.some((gate) => gate.kind === 'operator_review' && gate.fulfilled === false))
    .filter((i) => i.status === 'awaiting_operator')
    .sort((a, b) => b.priority_score - a.priority_score);

  return (
    <section className="runtime-block autonomy-pane autonomy-pane--approvals">
      <div className="runtime-block__title">
        Awaiting your approval
        <span className="t-meta" style={{ marginLeft: 8 }}>{awaiting.length}</span>
      </div>

      {awaiting.length === 0 ? (
        <p className="t-meta">Nothing pending — no agenda item needs operator input.</p>
      ) : (
        <ul className="autonomy-list">
          {awaiting.map((item) => {
            const open = item.approvals_required.filter((g) => !g.fulfilled);
            const busy = pending.has(item.id);
            return (
              <li
                key={item.id}
                className={`autonomy-row autonomy-row--awaiting_operator autonomy-row--clickable${busy ? ' is-busy' : ''}`}
                onClick={() => openDetail(item.id)}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    openDetail(item.id);
                  }
                }}
              >
                <div className="autonomy-row__head">
                  <span className="autonomy-chip autonomy-chip--awaiting_operator">awaiting</span>
                  <span className={`autonomy-chip autonomy-chip--risk-${item.risk_class}`}>
                    {item.risk_class}
                  </span>
                  <span className="autonomy-chip autonomy-chip--source">{item.source}</span>
                </div>
                <div className="autonomy-row__goal">{item.goal}</div>
                {item.rationale && (
                  <div className="autonomy-row__rationale t-meta">{item.rationale}</div>
                )}
                {open.length > 0 && (
                  <div className="autonomy-row__gates t-meta">
                    gates: {open.map((g) => `${g.kind}(${g.target})`).join(' · ')}
                  </div>
                )}
                <div
                  className="autonomy-row__actions"
                  onClick={(e) => e.stopPropagation()}
                >
                  <button
                    type="button"
                    className="autonomy-btn autonomy-btn--primary"
                    onClick={() => void approveItem(item.id)}
                    disabled={busy}
                  >
                    Approve
                  </button>
                  <button
                    type="button"
                    className="autonomy-btn autonomy-btn--danger"
                    onClick={() => void cancelItem(item.id)}
                    disabled={busy}
                  >
                    Reject
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
