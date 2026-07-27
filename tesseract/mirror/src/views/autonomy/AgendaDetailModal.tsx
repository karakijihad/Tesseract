// AU-7 S2 — Agenda item detail modal.
//
// Opened by clicking any agenda row. Renders:
//   - Header chips (status / risk / source)
//   - Goal text + full rationale
//   - Score components table (the deterministic AU-4 weights — operator
//     can audit how the rank was computed)
//   - Approval gates with fulfilment state
//   - Linked workers
//   - Status timeline (every transition with reason + actor)
//
// Mutating actions are mirrored from the parent panes — the modal is
// the canonical place to act on an item when there are more than three
// buttons worth surfacing. ESC + backdrop click close.

import React, { useEffect, useState } from 'react';
import type { AgendaItem } from '../../lib/api';
import { useAutonomyStore } from '../../stores/autonomy';

interface AgendaDetailModalProps {
  item: AgendaItem;
}

function _fmtIso(iso: string | null | undefined): string {
  if (!iso) return '—';
  const parsed = Date.parse(iso);
  if (Number.isNaN(parsed)) return iso;
  const d = new Date(parsed);
  return d.toISOString().replace('T', ' ').slice(0, 19) + 'Z';
}

export function AgendaDetailModal({ item }: AgendaDetailModalProps): React.ReactElement {
  const closeDetail = useAutonomyStore((s) => s.closeDetail);
  const approveItem = useAutonomyStore((s) => s.approveItem);
  const resumeItem = useAutonomyStore((s) => s.resumeItem);
  const cancelItem = useAutonomyStore((s) => s.cancelItem);
  const snoozeItem = useAutonomyStore((s) => s.snoozeItem);
  const boostItem = useAutonomyStore((s) => s.boostItem);
  const fetchComments = useAutonomyStore((s) => s.fetchAgendaComments);
  const postComment = useAutonomyStore((s) => s.postAgendaComment);
  const comments = useAutonomyStore((s) => s.agendaComments[item.id]);
  const commentStatus = useAutonomyStore((s) => s.agendaCommentStatus[item.id]);
  const commentError = useAutonomyStore((s) => s.agendaCommentError[item.id]);
  const pending = useAutonomyStore((s) => s.pendingActions);
  const busy = pending.has(item.id);

  const [draft, setDraft] = useState('');
  const [posting, setPosting] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closeDetail();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [closeDetail]);

  useEffect(() => {
    // Always refetch on (re)open — the WS broadcast covers live
    // sessions but a comment posted by another operator while this
    // modal was closed would otherwise stay invisible.
    void fetchComments(item.id);
  }, [item.id, fetchComments]);

  const onSubmitComment = async () => {
    const body = draft.trim();
    if (!body || posting) return;
    setPosting(true);
    try {
      const ok = await postComment(item.id, body);
      if (ok) setDraft('');
    } finally {
      setPosting(false);
    }
  };

  const components = Object.entries(item.score_components);
  const isAwaiting = item.status === 'awaiting_operator';
  const isBlocked = item.status === 'blocked';
  const isTerminal =
    item.status === 'done' ||
    item.status === 'cancelled' ||
    item.status === 'abandoned' ||
    item.status === 'superseded';
  return (
    <div
      className="autonomy-modal-backdrop"
      onClick={closeDetail}
    >
      <div
        className="autonomy-modal"
        onClick={(e) => e.stopPropagation()}
        data-testid="autonomy-detail-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="autonomy-modal-title"
      >
        <div className="autonomy-modal__head">
          <div className="autonomy-modal__title" id="autonomy-modal-title">
            <div className="autonomy-row__head">
              <span className={`autonomy-chip autonomy-chip--${item.status}`}>{item.status}</span>
              <span className={`autonomy-chip autonomy-chip--risk-${item.risk_class}`}>
                {item.risk_class}
              </span>
              <span className="autonomy-chip autonomy-chip--source">{item.source}</span>
              <span className="autonomy-row__score">score {item.priority_score.toFixed(1)}</span>
            </div>
            <div style={{ marginTop: 6 }}>{item.goal}</div>
            <div className="t-meta t-mono" style={{ marginTop: 4 }}>{item.id}</div>
          </div>
          <button
            type="button"
            className="autonomy-modal__close"
            onClick={closeDetail}
            aria-label="close detail"
          >
            ✕
          </button>
        </div>

        {item.rationale && (
          <div className="autonomy-modal__section">
            <div className="autonomy-modal__section-title">Rationale</div>
            <div>{item.rationale}</div>
          </div>
        )}

        {components.length > 0 && (
          <div className="autonomy-modal__section">
            <div className="autonomy-modal__section-title">Score breakdown</div>
            <div className="autonomy-modal__score">
              {components.map(([k, v]) => (
                <React.Fragment key={k}>
                  <div className="autonomy-modal__score-key">{k}</div>
                  <div className="autonomy-modal__score-val">
                    {typeof v === 'number' ? v.toFixed(2) : String(v)}
                  </div>
                </React.Fragment>
              ))}
              <div className="autonomy-modal__score-key">total</div>
              <div className="autonomy-modal__score-val">
                {item.priority_score.toFixed(2)}
              </div>
            </div>
          </div>
        )}

        {item.approvals_required.length > 0 && (
          <div className="autonomy-modal__section">
            <div className="autonomy-modal__section-title">Approval gates</div>
            <ul className="autonomy-list">
              {item.approvals_required.map((g, i) => (
                <li key={`${g.kind}-${i}`} className="autonomy-row">
                  <div className="autonomy-row__head">
                    <span
                      className={`autonomy-chip autonomy-chip--${g.fulfilled ? 'done' : 'awaiting_operator'}`}
                    >
                      {g.fulfilled ? 'fulfilled' : 'open'}
                    </span>
                    <span className="autonomy-chip autonomy-chip--source">{g.kind}</span>
                  </div>
                  <div className="autonomy-row__goal">{g.target}</div>
                  {g.fulfilled && g.fulfilled_at && (
                    <div className="t-meta">
                      fulfilled {_fmtIso(g.fulfilled_at)}
                      {g.fulfilled_by ? ` by ${g.fulfilled_by}` : ''}
                    </div>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}

        {item.linked_workers.length > 0 && (
          <div className="autonomy-modal__section">
            <div className="autonomy-modal__section-title">Linked work</div>
            <div className="t-meta t-mono">
              workers: {item.linked_workers.join(' · ')}
            </div>
          </div>
        )}

        {item.status_history.length > 0 && (
          <div className="autonomy-modal__section">
            <div className="autonomy-modal__section-title">
              Timeline ({item.status_history.length})
            </div>
            <ul className="autonomy-list">
              {item.status_history.slice(-8).reverse().map((t, i) => (
                <li key={`${t.at}-${i}`} className="autonomy-row">
                  <div className="autonomy-row__head">
                    <span className="autonomy-chip autonomy-chip--decision">
                      {(t.from_status ?? '∅') + ' → ' + t.to_status}
                    </span>
                    <span className="autonomy-chip autonomy-chip--source">{t.by}</span>
                    <span className="t-meta autonomy-row__score">{_fmtIso(t.at)}</span>
                  </div>
                  {t.reason && (
                    <div className="autonomy-row__rationale t-meta">{t.reason}</div>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="autonomy-modal__section">
          <div className="autonomy-modal__section-title">
            Comments {comments && comments.length > 0 ? `(${comments.length})` : ''}
          </div>
          {commentStatus === 'error' && (
            <p className="t-meta">Failed to load thread: {commentError}</p>
          )}
          {(comments?.length ?? 0) === 0 && commentStatus !== 'loading' && (
            <p className="t-meta">No comments yet. Ask a clarifying question — operator-visible.</p>
          )}
          {(comments?.length ?? 0) > 0 && (
            <ul className="autonomy-list" style={{ maxHeight: 240, overflowY: 'auto' }}>
              {comments!.map((c) => (
                <li key={c.id} className={`autonomy-row autonomy-row--${c.role === 'agent' ? 'running' : 'awaiting_operator'}`}>
                  <div className="autonomy-row__head">
                    <span className={`autonomy-chip autonomy-chip--${c.role === 'agent' ? 'kind' : 'source'}`}>
                      {c.role}
                    </span>
                    <span className="t-meta">{c.by}</span>
                    <span className="t-meta autonomy-row__score">{_fmtIso(c.at)}</span>
                  </div>
                  <div className="autonomy-row__goal" style={{ whiteSpace: 'pre-wrap' }}>{c.body}</div>
                </li>
              ))}
            </ul>
          )}
          {!isTerminal && (
            <div style={{ marginTop: 8 }}>
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
                    e.preventDefault();
                    void onSubmitComment();
                  }
                }}
                placeholder="Ask a clarifying question or leave a note. Cmd/Ctrl-Enter to send."
                rows={3}
                disabled={posting}
                style={{ width: '100%', resize: 'vertical', fontFamily: 'inherit', fontSize: 'inherit' }}
              />
              <div className="autonomy-row__actions" style={{ marginTop: 4 }}>
                <button
                  type="button"
                  className="autonomy-btn autonomy-btn--primary"
                  onClick={() => void onSubmitComment()}
                  disabled={posting || !draft.trim()}
                >
                  {posting ? 'Posting…' : 'Post comment'}
                </button>
              </div>
            </div>
          )}
        </div>

        {!isTerminal && (
          <div className="autonomy-modal__section">
            <div className="autonomy-row__actions" style={{ marginTop: 0 }}>
              {isAwaiting && (
                <button
                  type="button"
                  className="autonomy-btn autonomy-btn--primary"
                  onClick={() => void approveItem(item.id)}
                  disabled={busy}
                >
                  Approve
                </button>
              )}
              {isBlocked && (
                <button
                  type="button"
                  className="autonomy-btn autonomy-btn--primary"
                  onClick={() => void resumeItem(item.id)}
                  disabled={busy}
                  title="Re-queue this item — kernel dispatches a fresh worker on next tick. Raise agenda.yaml::worker_timeouts first if the prior worker hit its wallclock budget."
                >
                  Resume
                </button>
              )}
              <button
                type="button"
                className="autonomy-btn"
                onClick={() => void boostItem(item.id)}
                disabled={busy}
              >
                Boost
              </button>
              <button
                type="button"
                className="autonomy-btn"
                onClick={() => void snoozeItem(item.id)}
                disabled={busy}
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
          </div>
        )}
      </div>
    </div>
  );
}
