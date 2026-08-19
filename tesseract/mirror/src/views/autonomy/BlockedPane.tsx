// AU-7 — BlockedPane.
//
// Two related signals: agenda items in ``blocked`` status, and Governor
// source-pauses (loop / cost-spiral / trust-degradation triggers).
// Surfacing both in one pane prevents the operator from having to
// cross-reference two surfaces to answer §15 Q6 (what failed and when?).
// S2: per-pause Unpause button + per-blocked-item Cancel button.

import { Block } from '../../components/common/Block';
import React from 'react';
import type { AgendaItem, GovernorPause } from '../../lib/api';
import { useAutonomyStore } from '../../stores/autonomy';
import { Hint } from '../../components/ui/Hint';
import { Button } from '../../components/common/Button';
import { Row, RowActions } from '../../components/common/Row';

interface BlockedPaneProps {
  items: AgendaItem[];
  pauses: GovernorPause[];
  itemsStatus: 'idle' | 'loading' | 'ready' | 'error';
  governorStatus: 'idle' | 'loading' | 'ready' | 'error';
  itemsError: string | null;
  governorError: string | null;
}

export function BlockedPane({
  items,
  pauses,
  itemsStatus,
  governorStatus,
  itemsError,
  governorError,
}: BlockedPaneProps): React.ReactElement {
  const unpauseSource = useAutonomyStore((s) => s.unpauseSource);
  const cancelItem = useAutonomyStore((s) => s.cancelItem);
  const resumeItem = useAutonomyStore((s) => s.resumeItem);
  const openDetail = useAutonomyStore((s) => s.openDetail);
  const pending = useAutonomyStore((s) => s.pendingActions);

  const blocked = items
    .filter((i) => i.status === 'blocked')
    .sort((a, b) => b.priority_score - a.priority_score);

  const anyError = itemsStatus === 'error' || governorStatus === 'error';

  return (
    <Block title={null} meta={<>{blocked.length} blocked · {pauses.length} paused</>}>

      {anyError && (
        <p className="t-meta">
          Partial load — items: {itemsError ?? itemsStatus} · governor: {governorError ?? governorStatus}
        </p>
      )}

      {blocked.length === 0 && pauses.length === 0 && !anyError ? (
        <p className="t-meta">Nothing blocked, no sources paused.</p>
      ) : null}

      {pauses.length > 0 && (
        <div className="autonomy-pane__group">
          <div className="autonomy-pane__group-title">Paused sources</div>
          <ul className="autonomy-list">
            {pauses.map((p) => {
              const busy = pending.has(`pause:${p.source}`);
              return (
                <li key={p.source} className={`autonomy-row autonomy-row--pause${busy ? ' is-busy' : ''}`}>
                  <div className="autonomy-row__head">
                    <span className="autonomy-chip autonomy-chip--pause">paused</span>
                    <span className="autonomy-chip autonomy-chip--detector">{p.detector}</span>
                    <span className="autonomy-chip autonomy-chip--source">{p.source}</span>
                  </div>
                  <div className="autonomy-row__goal">{p.reason}</div>
                  <div className="autonomy-row__actions">
                    <Button
                      tone="primary"
                      onClick={() => void unpauseSource(p.source)}
                      disabled={busy}
                    >
                      Unpause
                    </Button>
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {blocked.length > 0 && (
        <div className="autonomy-pane__group">
          <div className="autonomy-pane__group-title">Blocked items</div>
          <ul className="autonomy-list">
            {blocked.slice(0, 6).map((item) => {
              const busy = pending.has(item.id);
              return (
                <Row
                  as="li"
                  key={item.id}
                  className={`autonomy-row autonomy-row--blocked${busy ? ' is-busy' : ''}`}
                  onClick={() => openDetail(item.id)}
                  ariaLabel={`Open ${item.goal}`}
                >
                  <div className="autonomy-row__head">
                    <span className="autonomy-chip autonomy-chip--blocked">blocked</span>
                    <span className="autonomy-chip autonomy-chip--source">{item.source}</span>
                  </div>
                  <div className="autonomy-row__goal">{item.goal}</div>
                  {item.blocked_reason && (
                    <div className="autonomy-row__rationale t-meta">{item.blocked_reason}</div>
                  )}
                  <RowActions className="autonomy-row__actions">
                    <Hint label="Re-queue this item — kernel will dispatch a fresh worker on next tick. Raise agenda.yaml::worker_timeouts first if the prior worker hit its wallclock budget.">
                      <Button
                        tone="primary"
                        onClick={() => void resumeItem(item.id)}
                        disabled={busy}
                      >
                        Resume
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
            {blocked.length > 6 && (
              <li className="t-meta">…{blocked.length - 6} more</li>
            )}
          </ul>
        </div>
      )}
    </Block>
  );
}
