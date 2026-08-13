// AU-7 — AgendaPane.
//
// Surfaces every non-terminal agenda item, ranked desc by priority_score.
// Answers GOVERNANCE §15 Q1 (what is the assistant doing) + Q2 (why) + Q3 (what
// can it do without approval). Score breakdown is rendered inline so the
// operator can audit the deterministic weights without opening a modal.
//
// S2: per-row Snooze / Boost / Cancel buttons. Clicking the row body
// (anywhere outside a button) opens the detail modal.

import React from 'react';
import type { AgendaItem } from '../../lib/api';
import { Markdown } from '../../components/common/Markdown';
import { useAutonomyStore } from '../../stores/autonomy';

interface AgendaPaneProps {
  items: AgendaItem[];
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
}

const ACTIVE_STATUSES = new Set([
  'proposed',
  'selected',
  'running',
  'resume_queued',
]);

const STATUS_LABEL: Record<string, string> = {
  proposed: 'proposed',
  selected: 'selected',
  running: 'running',
  resume_queued: 'resume-queued',
  awaiting_operator: 'awaiting-operator',
  blocked: 'blocked',
};

const RISK_LABEL: Record<string, string> = {
  autonomous: 'auto',
  propose: 'propose',
  operator_gate: 'gate',
  absolute_deny: 'deny',
};

// AU-23 — strategist rows get a distinctive label + accent chip class so
// operator-attended portfolio initiatives stand out from ambient sources.
const SOURCE_LABEL: Record<string, string> = {
  strategist: 'Strategist',
};

function _fmtScore(score: number): string {
  return score.toFixed(1);
}

export function AgendaPane({ items, status, error }: AgendaPaneProps): React.ReactElement {
  const snoozeItem = useAutonomyStore((s) => s.snoozeItem);
  const boostItem = useAutonomyStore((s) => s.boostItem);
  const cancelItem = useAutonomyStore((s) => s.cancelItem);
  const openDetail = useAutonomyStore((s) => s.openDetail);
  const pending = useAutonomyStore((s) => s.pendingActions);

  const active = items
    .filter((i) => ACTIVE_STATUSES.has(i.status))
    .sort((a, b) => b.priority_score - a.priority_score);
  const unvettedCount = items.filter((i) => i.status === 'unvetted').length;

  if (status === 'error') {
    return (
      <section className="runtime-block autonomy-pane autonomy-pane--agenda">
        <div className="runtime-block__title">Agenda</div>
        <p className="t-meta">Failed to load: {error}</p>
      </section>
    );
  }

  if (status !== 'ready' && active.length === 0) {
    return (
      <section className="runtime-block autonomy-pane autonomy-pane--agenda">
        <div className="runtime-block__title">Agenda</div>
        <p className="t-meta">Loading…</p>
      </section>
    );
  }

  return (
    <section className="runtime-block autonomy-pane autonomy-pane--agenda">
      <div className="runtime-block__title">
        Agenda
        <span className="t-meta" style={{ marginLeft: 8 }}>{active.length} active</span>
        {unvettedCount > 0 && (
          <span className="t-meta" style={{ marginLeft: 8 }}>{unvettedCount} pending vet</span>
        )}
      </div>

      {active.length === 0 ? (
        <p className="t-meta">No active items — nothing running. New work lands here as mappers fire.</p>
      ) : (
        <ul className="autonomy-list">
          {active.slice(0, 10).map((item) => {
            const busy = pending.has(item.id);
            return (
              <li
                key={item.id}
                className={`autonomy-row autonomy-row--${item.status} autonomy-row--clickable${busy ? ' is-busy' : ''}`}
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
                  <span className={`autonomy-chip autonomy-chip--${item.status}`}>
                    {STATUS_LABEL[item.status] ?? item.status}
                  </span>
                  <span className={`autonomy-chip autonomy-chip--risk-${item.risk_class}`}>
                    {RISK_LABEL[item.risk_class] ?? item.risk_class}
                  </span>
                  <span className={`autonomy-chip autonomy-chip--source autonomy-chip--src-${item.source}`}>
                    {SOURCE_LABEL[item.source] ?? item.source}
                  </span>
                  <span className="autonomy-row__score">{_fmtScore(item.priority_score)}</span>
                </div>
                <div className="autonomy-row__goal"><Markdown variant="inline">{item.goal}</Markdown></div>
                {item.rationale && (
                  <div className="autonomy-row__rationale t-meta"><Markdown variant="inline">{item.rationale}</Markdown></div>
                )}
                {Object.keys(item.score_components).length > 0 && (
                  <div className="autonomy-row__components t-meta">
                    {Object.entries(item.score_components)
                      .map(([k, v]) => `${k}=${typeof v === 'number' ? v.toFixed(1) : v}`)
                      .join(' · ')}
                  </div>
                )}
                <div
                  className="autonomy-row__actions"
                  onClick={(e) => e.stopPropagation()}
                >
                  <button
                    type="button"
                    className="autonomy-btn"
                    onClick={() => void boostItem(item.id)}
                    disabled={busy}
                    title="Operator priority +5"
                  >
                    Boost
                  </button>
                  <button
                    type="button"
                    className="autonomy-btn"
                    onClick={() => void snoozeItem(item.id)}
                    disabled={busy}
                    title="Operator priority −2"
                  >
                    Snooze
                  </button>
                  <button
                    type="button"
                    className="autonomy-btn autonomy-btn--danger"
                    onClick={() => void cancelItem(item.id)}
                    disabled={busy}
                  >
                    Cancel
                  </button>
                </div>
              </li>
            );
          })}
          {active.length > 10 && (
            <li className="t-meta">…{active.length - 10} more</li>
          )}
        </ul>
      )}
    </section>
  );
}
