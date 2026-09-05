// One agenda item, opened a level down inside the pane.
//
// The same card shape every level 3 on this panel uses: the thing's own
// sentence, a grid of facts, then bands of state lines. It drew a modal head,
// four chips, a score grid of its own and five sections of bordered cards
// until 2026-08-25, which is the shape the panel replaced everywhere else.
//
// The only words this file authors are field names and control names. What the
// item IS, why it was proposed and what each transition meant are the kernel's
// own text, rendered as it was written.

import React, { useEffect, useState } from 'react';
import type { AgendaItem } from '../../lib/api';
import { Markdown } from '../../components/common/Markdown';
import { useAutonomyStore } from '../../stores/autonomy';
import { Button } from '../../components/common/Button';
import { Fact } from '../../components/common/Fact';
import { Note } from '../../components/common/Note';
import { Textarea } from '../../components/common/Textarea';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
import { clock } from '../../lib/time';

// How many transitions are worth showing. The whole history is in the record;
// what a person reads on a card is how it got to where it is.
const TIMELINE = 8;

// A status that means the item is finished, one way or another. Nothing may be
// done to it, so nothing is offered.
const TERMINAL = new Set(['done', 'cancelled', 'abandoned', 'superseded', 'failed']);

// A transition into one of these did not go the way it was meant to.
const BADLY = new Set(['failed', 'cancelled', 'abandoned', 'blocked']);

export function AgendaDetail({ item }: { item: AgendaItem }): React.ReactElement {
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
    // Always re-read on open. The socket covers a live session, but a comment
    // left by somebody else while this was closed would stay invisible.
    void fetchComments(item.id);
  }, [item.id, fetchComments]);

  const onSubmitComment = async () => {
    const body = draft.trim();
    if (!body || posting) return;
    setPosting(true);
    try {
      if (await postComment(item.id, body)) setDraft('');
    } finally {
      setPosting(false);
    }
  };

  const scored = Object.entries(item.score_components);
  const isAwaiting = item.status === 'awaiting_operator';
  const isBlocked = item.status === 'blocked';
  const isTerminal = TERMINAL.has(item.status);
  const thread = comments ?? [];

  const gates: StateLine[] = item.approvals_required.map((gate, i) => ({
    key: `${gate.kind}:${i}`,
    state: gate.fulfilled ? 'idle' : 'pending',
    label: gate.fulfilled ? 'answered' : 'waiting on you',
    name: gate.kind,
    said: <Markdown variant="inline">{gate.target}</Markdown>,
    when: gate.fulfilled ? clock(gate.fulfilled_at) : '',
    value: gate.fulfilled && gate.fulfilled_by ? `by ${gate.fulfilled_by}` : '',
  }));

  const timeline: StateLine[] = item.status_history
    .slice(-TIMELINE)
    .reverse()
    .map((t, i) => ({
      key: `${t.at}:${i}`,
      state: BADLY.has(t.to_status) ? 'degraded' : 'idle',
      label: t.to_status.replace(/_/g, ' '),
      name: t.by,
      said: t.reason ? <Markdown variant="inline">{t.reason}</Markdown> : '',
      when: clock(t.at),
      value: t.to_status.replace(/_/g, ' '),
    }));

  return (
    <div className="entry-card" data-testid="autonomy-detail">
      <p className="entry-card__summary">
        <Markdown variant="inline">{item.goal}</Markdown>
      </p>

      {item.rationale && (
        <>
          <Band label="Why it was proposed" />
          <div className="entry-card__why">
            <Markdown>{item.rationale}</Markdown>
          </div>
        </>
      )}

      <Band label="What it is" />
      <div className="entry-card__facts">
        <Fact label="Doing">{item.status.replace(/_/g, ' ')}</Fact>
        <Fact label="Came from">{item.source}</Fact>
        <Fact label="Risk">{item.risk_class.replace(/_/g, ' ')}</Fact>
        <Fact label="Rank">{item.priority_score.toFixed(1)}</Fact>
        {item.linked_workers.length > 0 && (
          <Fact label="Workers">{item.linked_workers.join(', ')}</Fact>
        )}
        {item.turn_ids.length > 0 && <Fact label="Turns">{item.turn_ids.join(', ')}</Fact>}
        {item.attempts.length > 0 && <Fact label="Tries">{item.attempts.length}</Fact>}
        {item.blocked_reason && <Fact label="Held by">{item.blocked_reason}</Fact>}
      </div>

      {item.success_criteria && (
        <>
          <Band label="What would count as done" />
          <div className="entry-card__why">
            <Markdown>{item.success_criteria}</Markdown>
          </div>
        </>
      )}

      {item.verification && (
        <>
          <Band label="What was found" />
          <div className="entry-card__why">
            <Markdown>{item.verification}</Markdown>
          </div>
        </>
      )}

      {scored.length > 0 && (
        <>
          <Band label="How that rank was reached" />
          <div className="entry-card__facts">
            {scored.map(([key, value]) => (
              <Fact key={key} label={key.replace(/_/g, ' ')}>
                {typeof value === 'number' ? value.toFixed(2) : String(value)}
              </Fact>
            ))}
            <Fact label="total">{item.priority_score.toFixed(2)}</Fact>
          </div>
        </>
      )}

      {gates.length > 0 && (
        <div className="autonomy-group">
          <Band label="What it is waiting to be allowed" count={gates.length} />
          <StateStrip lines={gates} />
        </div>
      )}

      {timeline.length > 0 && (
        <div className="autonomy-group">
          <Band label="How it got here" count={item.status_history.length} />
          <StateStrip lines={timeline} />
        </div>
      )}

      <div className="autonomy-group">
        <Band label="What was said about it" count={thread.length} />
        {commentStatus === 'error' && (
          <Note tone="bad">The thread could not be read. {commentError}</Note>
        )}
        {thread.length === 0 && commentStatus !== 'loading' && (
          <p className="t-meta">
            Nothing yet. Ask a question here and the answer lands in the same
            place.
          </p>
        )}
        {thread.length > 0 && (
          <StateStrip
            lines={thread.map((c) => ({
              key: c.id,
              state: c.role === 'agent' ? 'running' : 'idle',
              label: c.role,
              name: c.by,
              said: <Markdown variant="inline">{c.body}</Markdown>,
              when: clock(c.at),
              value: '',
            }))}
          />
        )}
        {!isTerminal && (
          <>
            <Textarea
              value={draft}
              onChange={setDraft}
              onKeyDown={(e) => {
                if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
                  e.preventDefault();
                  void onSubmitComment();
                }
              }}
              placeholder="Ask a question or leave a note. Cmd or Ctrl with Enter sends it."
              rows={3}
              disabled={posting}
            />
            <div className="entry-card__acts">
              <Button
                tone="primary"
                onClick={() => void onSubmitComment()}
                disabled={posting || !draft.trim()}
              >
                {posting ? 'sending' : 'send'}
              </Button>
            </div>
          </>
        )}
      </div>

      {!isTerminal && (
        <div className="entry-card__acts">
          {isAwaiting && (
            <Button
              tone="primary"
              onClick={() => void approveItem(item.id)}
              disabled={busy}
            >
              approve
            </Button>
          )}
          {isBlocked && (
            <Button
              tone="primary"
              onClick={() => void resumeItem(item.id)}
              disabled={busy}
            >
              resume
            </Button>
          )}
          <Button onClick={() => void boostItem(item.id)} disabled={busy}>
            do it sooner
          </Button>
          <Button onClick={() => void snoozeItem(item.id)} disabled={busy}>
            do it later
          </Button>
          <Button tone="danger" onClick={() => void cancelItem(item.id)} disabled={busy}>
            cancel
          </Button>
        </div>
      )}
    </div>
  );
}
