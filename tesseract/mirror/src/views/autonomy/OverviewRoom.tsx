// Overview. What the machine is doing on its own, and what it needs from you.
//
// The room the panel opens on, and the one the operator's own word named
// (ruling 10). Four figures over three bands, and nothing else: the reference
// build's Overview is `rail + body`, with no operations strip above it, which
// is what gives the middle band room to hold its rows without a scrollbar.
//
// **This file joins nothing and decides nothing.** The four producers behind
// "wants you" are joined in `routes/autonomy_overview.py`, because a list that
// ranks a failed step against a paused source is a reading of what those
// states mean, and the layer with no access to that meaning is this one. Every
// state, its words, the sentence beside each name and what may be done from
// the row all arrive in the payload.
//
// What IS written here is the name of a control and the name of a band. Those
// name what the operator can do and where they are, which is the view's job.

import { useEffect } from 'react';
import { Note } from '../../components/common/Note';
import { RowControls, type RowActs } from './RowControls';
import type { OverviewFigure, OverviewLine, OverviewResponse } from '../../lib/api';
import { useAutonomyStore } from '../../stores/autonomy';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
import { clock } from '../../lib/time';

// Nothing broadcasts a seat's elapsed time or a run's completion, so the room
// reconciles on its own clock. A minute is the slow end of the liveness
// contract's window and the figures are counts rather than a stopwatch.
const POLL_MS = 60_000;

/** What acting on a row does. Every one is a write on the store action that
 *  already owns it, and what the row says afterwards is re-read rather than
 *  decided here. */
export interface OverviewActs extends RowActs {
  open: (line: OverviewLine) => void;
}

export function Figures({ figures }: { figures: OverviewFigure[] }): React.ReactElement {
  return (
    <div className="autonomy-figures">
      {figures.map((fig) => (
        <div key={fig.key} className="autonomy-figure">
          <span
            className={`autonomy-figure__n${fig.warn ? ' autonomy-figure__n--warn' : ''}`}
          >
            {fig.value}
          </span>
          <span className="autonomy-figure__l t-meta t-label">{fig.label}</span>
        </div>
      ))}
    </div>
  );
}

function toLine(line: OverviewLine, band: string, acts: OverviewActs): StateLine {
  return {
    key: `${band}:${line.name}:${line.at ?? ''}`,
    state: line.state,
    obligation: line.obligation,
    label: line.label,
    name: line.name,
    said: line.said,
    when: clock(line.at),
    value: line.value,
    // Only what the payload gives somewhere to go. A row that cannot be
    // opened must not look like it can.
    onOpen: line.opens ? () => acts.open(line) : undefined,
    // A row with something to do is a row waiting on the operator, and this
    // room's first band is exactly those, so the controls stay on the screen.
    actions:
      line.acts.length > 0 ? (
        <RowControls line={line} acts={acts} waiting />
      ) : undefined,
  };
}

function Section({
  label,
  lines,
  total,
  acts,
  empty,
}: {
  label: string;
  lines: OverviewLine[];
  total: number;
  acts: OverviewActs;
  /** What the band says when it holds nothing. A band with an answer says it;
   *  a band with none is not drawn at all. */
  empty?: string;
}): React.ReactElement | null {
  if (lines.length === 0) {
    if (!empty) return null;
    return (
      <div className="autonomy-group">
        <Band label={label} />
        <p className="t-meta">{empty}</p>
      </div>
    );
  }
  return (
    <div className="autonomy-group">
      <Band label={label} count={total} />
      <StateStrip lines={lines.map((l) => toLine(l, label, acts))} />
      {total > lines.length && (
        <p className="t-meta">
          {total - lines.length} more are not listed here.
        </p>
      )}
    </div>
  );
}

export function OverviewRoomView({
  overview,
  status,
  error,
  acts,
}: {
  overview: OverviewResponse | null;
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
  acts: OverviewActs;
}): React.ReactElement {
  if (status === 'error') {
    return <Note tone="bad">What the machine is doing could not be read. {error}</Note>;
  }
  if (status !== 'ready' || overview === null) return <></>;

  return (
    <>
      <Figures figures={overview.figures} />
      <Section
        label="Wants you"
        lines={overview.wantsYou}
        total={overview.wantsYouTotal}
        acts={acts}
        empty="Nothing needs you."
      />
      <Section
        label="Owed to you"
        lines={overview.owed}
        total={overview.owed.length}
        acts={acts}
        empty="No task is open. Ask for one in a conversation and accept it there."
      />
      <Section
        label="Working now"
        lines={overview.workingNow}
        total={overview.workingNowTotal}
        acts={acts}
        empty="Nothing is running."
      />
      <Section
        label="Ran while you were away"
        lines={overview.ranWhileAway}
        total={overview.ranWhileAwayTotal}
        acts={acts}
      />
      {overview.aged > 0 && (
        // Counted out loud rather than dropped. A queue that quietly forgets
        // is the defect this room exists to prevent.
        <p className="t-meta">
          {overview.aged} finished more than two days ago and{' '}
          {overview.aged === 1 ? 'is' : 'are'} not listed here.
        </p>
      )}
    </>
  );
}

export function OverviewRoom(): React.ReactElement {
  const overview = useAutonomyStore((s) => s.overview);
  const fetchOverview = useAutonomyStore((s) => s.fetchOverview);
  const pushLevel = useAutonomyStore((s) => s.pushLevel);
  const approveItem = useAutonomyStore((s) => s.approveItem);
  const cancelItem = useAutonomyStore((s) => s.cancelItem);
  const resumeItem = useAutonomyStore((s) => s.resumeItem);
  const unpauseSource = useAutonomyStore((s) => s.unpauseSource);

  useEffect(() => {
    void fetchOverview();
    const timer = window.setInterval(() => void fetchOverview(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [fetchOverview]);

  // Every action is a write on the store action that already owns it, followed
  // by a re-read of this room. Nothing here decides what the row now says.
  const after = () => {
    void fetchOverview();
  };
  const acts: OverviewActs = {
    approve: (id) => void approveItem(id).then(after),
    cancel: (id) => void cancelItem(id).then(after),
    resume: (id) => void resumeItem(id).then(after),
    unpause: (source) => void unpauseSource(source).then(after),
    open: (line) => {
      if (!line.opens) return;
      pushLevel({
        kind: line.opens.kind as 'entry' | 'agenda' | 'worker',
        id: line.opens.id,
        label: line.name,
      });
    },
  };

  return (
    <OverviewRoomView
      overview={overview.data}
      status={overview.status}
      error={overview.error}
      acts={acts}
    />
  );
}
