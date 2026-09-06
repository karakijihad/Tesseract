// Blocked and paused. What is waiting on something outside it.
//
// Two bands, and this file decides neither. Which state a held item is in,
// what that state is called, what is true of it and what may be done to it all
// arrive in `GET /api/autonomy/overview`, which already performs this join for
// the Overview room's first band. Two readings of one join, both the
// backend's, so the two rooms cannot disagree about what a held item is.
//
// It used to draw bordered cards with three chips in the head of each and a
// row of always-visible buttons underneath, which is the shape the panel's
// redesign replaced everywhere else. A room here is a list of state lines.

import { Note } from '../../components/common/Note';
import { RowControls, type RowActs } from './RowControls';
import type { OverviewLine } from '../../lib/api';
import { useAutonomyStore } from '../../stores/autonomy';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
import { clock } from '../../lib/time';

/** What acting on a row does. Every one is a write on the store action that
 *  already owns it, followed by a re-read. */
export interface HeldActs extends RowActs {
  open: (line: OverviewLine) => void;
}

function toLine(line: OverviewLine, band: string, acts: HeldActs): StateLine {
  return {
    key: `${band}:${line.name}:${line.at ?? ''}`,
    state: line.state,
    obligation: line.obligation,
    label: line.label,
    name: line.name,
    said: line.said,
    when: clock(line.at),
    value: line.value,
    onOpen: line.opens ? () => acts.open(line) : undefined,
    // Every row in this room is waiting on something, so every remedy it
    // has is on the screen rather than under the pointer.
    actions:
      line.acts.length > 0 ? (
        <RowControls line={line} acts={acts} waiting />
      ) : undefined,
  };
}

export function BlockedPaneView({
  held,
  paused,
  status,
  error,
  acts,
}: {
  held: OverviewLine[];
  paused: OverviewLine[];
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
  acts: HeldActs;
}): React.ReactElement {
  if (status === 'error') {
    return <Note tone="bad">What is held could not be read. {error}</Note>;
  }
  if (status !== 'ready') return <></>;

  if (held.length === 0 && paused.length === 0) {
    return <p className="t-meta">Nothing is held, and no source is paused.</p>;
  }

  return (
    <>
      {paused.length > 0 && (
        <div className="autonomy-group">
          <Band label="Sources the governor paused" count={paused.length} />
          <StateStrip lines={paused.map((l) => toLine(l, 'paused', acts))} />
        </div>
      )}
      {held.length > 0 && (
        <div className="autonomy-group">
          <Band label="Held, waiting on something" count={held.length} />
          <StateStrip lines={held.map((l) => toLine(l, 'held', acts))} />
        </div>
      )}
    </>
  );
}

export function BlockedPane(): React.ReactElement {
  const overview = useAutonomyStore((s) => s.overview);
  const fetchOverview = useAutonomyStore((s) => s.fetchOverview);
  const pushLevel = useAutonomyStore((s) => s.pushLevel);
  const approveItem = useAutonomyStore((s) => s.approveItem);
  const resumeItem = useAutonomyStore((s) => s.resumeItem);
  const cancelItem = useAutonomyStore((s) => s.cancelItem);
  const unpauseSource = useAutonomyStore((s) => s.unpauseSource);

  const after = () => {
    void fetchOverview();
  };
  const acts: HeldActs = {
    approve: (id) => void approveItem(id).then(after),
    resume: (id) => void resumeItem(id).then(after),
    cancel: (id) => void cancelItem(id).then(after),
    unpause: (source) => void unpauseSource(source).then(after),
    open: (line) => {
      if (!line.opens) return;
      pushLevel({
        kind: line.opens.kind as 'agenda',
        id: line.opens.id,
        label: line.name,
      });
    },
  };

  return (
    <BlockedPaneView
      held={overview.data?.held ?? []}
      paused={overview.data?.paused ?? []}
      status={overview.status}
      error={overview.error}
      acts={acts}
    />
  );
}
