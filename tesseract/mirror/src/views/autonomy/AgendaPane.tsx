// AU-7 — AgendaPane.
//
// Surfaces every non-terminal agenda item, ranked desc by priority_score.
// Answers GOVERNANCE §15 Q1 (what is the assistant doing) + Q2 (why) + Q3 (what
// can it do without approval). Score breakdown is rendered inline so the
// operator can audit the deterministic weights without opening a modal.
//
// S2: per-row Snooze / Boost / Cancel buttons. Clicking the row body
// (anywhere outside a button) opens the detail modal.

import { Block } from '../../components/common/Block';
import React from 'react';
import type { AgendaItem } from '../../lib/api';
import { Markdown } from '../../components/common/Markdown';
import { useAutonomyStore } from '../../stores/autonomy';
import { Hint } from '../../components/ui/Hint';
import { Button } from '../../components/common/Button';
import { Row, RowActions } from '../../components/common/Row';

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

// A source whose raw enum value does not read well in a chip gets a label
// here. Empty today: the live sources — operator, recovery, provider_watch,
// follow_up — all read as themselves.
const SOURCE_LABEL: Record<string, string> = {};

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
      <Block title="Agenda">
        <p className="t-meta">Failed to load: {error}</p>
      </Block>
    );
  }

  if (status !== 'ready' && active.length === 0) {
    return (
      <Block title="Agenda">
        <p className="t-meta">Loading…</p>
      </Block>
    );
  }

  return (
    <Block
      title="Agenda"
      meta={
        <>
          {active.length} active
          {unvettedCount > 0 && ` · ${unvettedCount} pending vet`}
        </>
      }
    >

      {active.length === 0 ? (
        <p className="t-meta">No active items — nothing running. New work lands here as mappers fire.</p>
      ) : (
        <ul className="autonomy-list">
          {active.slice(0, 10).map((item) => {
            const busy = pending.has(item.id);
            return (
              <Row
                as="li"
                key={item.id}
                className={`autonomy-row autonomy-row--${item.status}${busy ? ' is-busy' : ''}`}
                onClick={() => openDetail(item.id)}
                ariaLabel={`Open ${item.goal}`}
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
                <RowActions className="autonomy-row__actions">
                  <Hint label="Operator priority +5">
                    <Button
                      onClick={() => void boostItem(item.id)}
                      disabled={busy}
                    >
                      Boost
                    </Button>
                  </Hint>
                  <Hint label="Operator priority −2">
                    <Button
                      onClick={() => void snoozeItem(item.id)}
                      disabled={busy}
                    >
                      Snooze
                    </Button>
                  </Hint>
                  <Button
                    tone="danger"
                    onClick={() => void cancelItem(item.id)}
                    disabled={busy}
                  >
                    Cancel
                  </Button>
                </RowActions>
              </Row>
            );
          })}
          {active.length > 10 && (
            <li className="t-meta">…{active.length - 10} more</li>
          )}
        </ul>
      )}
    </Block>
  );
}
