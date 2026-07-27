// AU-7 — BlockedPane.
//
// Two related signals: agenda items in ``blocked`` status, and Governor
// source-pauses (loop / cost-spiral / trust-degradation triggers).
// Surfacing both in one pane prevents the operator from having to
// cross-reference two surfaces to answer §15 Q6 (what failed and when?).
// S2: per-pause Unpause button + per-blocked-item Cancel button.

import React from 'react';
import type { AgendaItem, GovernorPause } from '../../lib/api';
import { useAutonomyStore } from '../../stores/autonomy';

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
    <section className="runtime-block autonomy-pane autonomy-pane--blocked">
      <div className="runtime-block__title">
        Blocked & paused sources
        <span className="t-meta" style={{ marginLeft: 8 }}>
          {blocked.length} blocked · {pauses.length} paused
        </span>
      </div>

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
                    <button
                      type="button"
                      className="autonomy-btn autonomy-btn--primary"
                      onClick={() => void unpauseSource(p.source)}
                      disabled={busy}
                    >
                      Unpause
                    </button>
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
                <li
                  key={item.id}
                  className={`autonomy-row autonomy-row--blocked autonomy-row--clickable${busy ? ' is-busy' : ''}`}
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
                    <span className="autonomy-chip autonomy-chip--blocked">blocked</span>
                    <span className="autonomy-chip autonomy-chip--source">{item.source}</span>
                  </div>
                  <div className="autonomy-row__goal">{item.goal}</div>
                  {item.blocked_reason && (
                    <div className="autonomy-row__rationale t-meta">{item.blocked_reason}</div>
                  )}
                  <div
                    className="autonomy-row__actions"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <button
                      type="button"
                      className="autonomy-btn autonomy-btn--primary"
                      onClick={() => void resumeItem(item.id)}
                      disabled={busy}
                      title="Re-queue this item — kernel will dispatch a fresh worker on next tick. Raise agenda.yaml::worker_timeouts first if the prior worker hit its wallclock budget."
                    >
                      Resume
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
            {blocked.length > 6 && (
              <li className="t-meta">…{blocked.length - 6} more</li>
            )}
          </ul>
        </div>
      )}
    </section>
  );
}
