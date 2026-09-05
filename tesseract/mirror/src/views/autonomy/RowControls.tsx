// What may be done to one row, drawn once.
//
// Overview's first band and Blocked and paused list the same held items, out
// of the same join, and each had written its own copy of this. They had drifted
// where it mattered most: `held_items` answers `approve` for anything waiting
// on the operator, and the Blocked copy never drew that button, so the room
// that exists for things waiting on a decision could not take one.
//
// **A row that is waiting on you shows its controls without being hovered.**
// The reveal-on-hover is right for a long list where acting is occasional, and
// wrong here: the operator read every held item, saw a reason and no next step,
// and said so. The house copy rule is say the consequence, then the remedy, and
// a remedy you have to find with the pointer has not been offered.

import { Button } from '../../components/common/Button';
import { RowActions } from '../../components/common/Row';
import type { OverviewLine } from '../../lib/api';

/** What acting on a row does. Every one is a write on the store action that
 *  already owns it, followed by a re-read: what the row says afterwards is the
 *  backend's answer, never one composed from what was sent. */
export interface RowActs {
  approve: (id: string) => void;
  resume: (id: string) => void;
  unpause: (source: string) => void;
  cancel: (id: string) => void;
}

export function RowControls({
  line,
  acts,
  waiting = false,
}: {
  line: OverviewLine;
  acts: RowActs;
  /** This row is waiting on a decision, so its controls stay visible. */
  waiting?: boolean;
}): React.ReactElement {
  const id = line.opens?.id ?? line.name;
  // `RowActions` rather than a span of buttons: it is what stops a click on
  // `approve` from also being a click on the row, which opened the item
  // behind every action until Managed system found it.
  return (
    <RowActions className={`state-acts${waiting ? ' state-acts--waiting' : ''}`}>
      {line.acts.includes('approve') && (
        <Button onClick={() => acts.approve(id)} ariaLabel={`Approve ${line.name}`}>
          approve
        </Button>
      )}
      {line.acts.includes('resume') && (
        <Button onClick={() => acts.resume(id)} ariaLabel={`Resume ${line.name}`}>
          resume
        </Button>
      )}
      {line.acts.includes('unpause') && (
        // `actsOn`, not `name`: the row says the source in words and the route
        // is keyed by its identifier. Posting what the row is called would
        // ask the backend to start something with no such name.
        <Button
          onClick={() => acts.unpause(line.actsOn ?? line.name)}
          ariaLabel={`Unpause ${line.name}`}
        >
          unpause
        </Button>
      )}
      {line.acts.includes('cancel') && (
        <Button tone="danger" onClick={() => acts.cancel(id)} ariaLabel={`Cancel ${line.name}`}>
          cancel
        </Button>
      )}
    </RowActions>
  );
}
