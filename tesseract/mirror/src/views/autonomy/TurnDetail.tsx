// One recovered turn, opened a level down inside the pane.
//
// The record was never this room's own: a turn's steps, tools and outcome
// are the Conscience panel's Day record, read by `/api/conscience/day` and
// drawn by `TurnRecord`. Recovery names which day a turn closed into (or, for
// one whose manifest could not be read at boot, names none), and this reads
// that same day back and hands the one matching turn to the same renderer
// the Conscience panel uses, rather than telling a second version of it.

import { useEffect, useState } from 'react';
import { Fact } from '../../components/common/Fact';
import { Note } from '../../components/common/Note';
import { clock } from '../../lib/time';
import { BACKEND_BASE } from '../../lib/endpoints';
import type { DayReport } from '../../stores/conscience';
import { TurnRecord, OutcomeWord } from '../conscience/TurnRecord';

type LoadState = 'loading' | 'ready' | 'error' | 'unreadable';

export function TurnDetail({
  id,
  day,
}: {
  id: string;
  day?: string;
}): React.ReactElement {
  const [state, setState] = useState<LoadState>(day ? 'loading' : 'unreadable');
  const [report, setReport] = useState<DayReport | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!day) {
      setState('unreadable');
      return undefined;
    }
    let cancelled = false;
    setState('loading');
    // A DIRECT fetch of the same reader the Conscience panel uses, not
    // `useConscienceStore.fetchDay()`. That store's `day`/`dayOn` are the
    // Conscience Day panel's OWN "which day is on screen" pointer, not a data
    // cache, and calling it from here would silently rewrite what that panel
    // shows the next time the operator opens it, over a turn they were never
    // browsing. Reading the endpoint straight, into local state, gets the
    // same record without reaching into a pointer this room does not own. Do
    // not fold this into the store call to remove the "duplication": the
    // duplication is the point, and the shared state it would create is
    // exactly the class of bug this panel exists to not have.
    fetch(`${BACKEND_BASE}/api/conscience/day?on=${encodeURIComponent(day)}`)
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json() as Promise<DayReport>;
      })
      .then((data) => {
        if (cancelled) return;
        setReport(data);
        setState('ready');
      })
      .catch((err) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : String(err));
        setState('error');
      });
    return () => {
      cancelled = true;
    };
  }, [day, id]);

  if (state === 'unreadable') {
    return (
      <Note tone="warn">
        This turn&apos;s record could not be read at all, so there is nothing
        to open. The reason above is everything known about it.
      </Note>
    );
  }
  if (state === 'loading') {
    return <p className="t-meta">Reading the day this turn closed into.</p>;
  }
  if (state === 'error') {
    return <Note tone="bad">The day could not be read: {error}</Note>;
  }

  const turn = report?.turns.find((t) => t.turn === id);
  if (!turn) {
    return (
      <Note tone="warn">
        This turn is no longer in that day&apos;s record. Either its closing
        write failed at the time, or it has since aged out of the days the
        runtime keeps.
      </Note>
    );
  }

  return (
    <div className="entry-card" data-testid="turn-detail">
      <p className="entry-card__summary">{turn.label}</p>
      <div className="entry-card__facts">
        <Fact label="Started">{clock(turn.at)}</Fact>
        <Fact label="Came in through">{turn.door}</Fact>
        <Fact label="Outcome">
          <OutcomeWord outcome={turn.outcome} />
        </Fact>
      </div>
      <TurnRecord turn={turn} calls={report?.calls ?? []} />
    </div>
  );
}
