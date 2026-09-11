// One recovered effect, opened a level down inside the pane.
//
// What recovery could not close on its own: a call the last process made and
// never recorded the end of. It already filed one question about this, as the
// same clarification card the inbox uses, and this reads that card rather
// than telling a second version of it.

import { useEffect, useState } from 'react';
import { useWorkspaceStore } from '../../stores/workspace';
import { EventDetailBody } from '../workspace/EventDetailBody';
import { CommentThread } from '../workspace/CommentThread';
import { Note } from '../../components/common/Note';
import { Button } from '../../components/common/Button';
import { Hint } from '../../components/ui/Hint';

/** The one place this frontend turns a call's id into its card's id. The
 *  card's id is DERIVED from the call's, never carried as an identity of its
 *  own, and the backend makes that same decision in one place,
 *  `effects.py::card_id_for`. A second inline template anywhere else in this
 *  tree would be a second answer that can drift from it; import this instead. */
export function effectCardId(callId: string): string {
  return `effect-${callId}`;
}

export function EffectDetail({ callId }: { callId: string }): React.ReactElement {
  const cardId = effectCardId(callId);
  const event = useWorkspaceStore(
    (s) =>
      s.events.find((e) => e.event_id === cardId) ??
      s.history.find((e) => e.event_id === cardId) ??
      null,
  );
  const refreshEvent = useWorkspaceStore((s) => s.refreshEvent);
  const decide = useWorkspaceStore((s) => s.decide);
  const [notFound, setNotFound] = useState(false);

  // The store may already hold this card (the operator had the inbox open
  // this session) or may not (opened straight from Recovery). Only asked for
  // when it is missing, and never re-asked once found: a live comment lands
  // through the socket dispatcher the same as it does in the inbox.
  useEffect(() => {
    if (event) return undefined;
    let cancelled = false;
    void refreshEvent(cardId).then((found) => {
      if (!cancelled && !found) setNotFound(true);
    });
    return () => {
      cancelled = true;
    };
  }, [cardId, event, refreshEvent]);

  if (!event) {
    if (notFound) {
      return (
        <Note tone="warn">
          This card is no longer in the inbox, so there is nothing to answer
          here. Check History in the inbox to see what happened to it.
        </Note>
      );
    }
    return <p className="t-meta">Reading what recovery asked about this.</p>;
  }

  return (
    <div className="entry-card" data-testid="effect-detail">
      <p className="entry-card__summary">{event.summary}</p>
      <EventDetailBody event={event} />
      <CommentThread event_id={event.event_id} comments={event.comments} />
      {event.status === 'pending' ? (
        <Hint label="Marks this settled without repeating the call or checking it. Say what you decided in the thread above first.">
          <Button onClick={() => void decide(event.event_id, 'resolve')}>
            Resolve
          </Button>
        </Hint>
      ) : (
        <p className="t-meta">This was already {event.status}.</p>
      )}
    </div>
  );
}
