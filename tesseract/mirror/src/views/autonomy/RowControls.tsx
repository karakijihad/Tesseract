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
import { Hint } from '../../components/ui/Hint';
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

/** What each control actually sends, read into a `Hint` beside it because
 *  none of the five labels below says its own consequence.
 *
 *  Cancel especially: the word alone does not say whether it stops a running
 *  turn, withdraws a request, or throws work away. It does none of the
 *  first. `postCancelAgendaItem` moves the agenda item straight to its
 *  terminal `cancelled` status; it never touches whatever conversation may
 *  already be working the item, which keeps running until that turn ends on
 *  its own. What cancel changes is what the runtime expects next: nothing. */
const APPROVE_SAYS =
  'Clears the approval this is waiting on, so it runs once, right now.';
const RESUME_SAYS =
  'Puts this back in the queue with a fresh attempt on the same goal. What was tried before stays on record.';
const UNPAUSE_SAYS = 'Lets this source send work to the runtime again.';
const REMOVE_SAYS =
  'Clears this row. Its source is gone, so there is nothing left to start again.';
const CANCEL_SAYS =
  'Marks this closed for good. It does not stop a turn already working on it, only what happens next.';

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
        <Hint label={APPROVE_SAYS}>
          <Button onClick={() => acts.approve(id)} ariaLabel={`Approve ${line.name}`}>
            approve
          </Button>
        </Hint>
      )}
      {line.acts.includes('resume') && (
        <Hint label={RESUME_SAYS}>
          <Button onClick={() => acts.resume(id)} ariaLabel={`Resume ${line.name}`}>
            resume
          </Button>
        </Hint>
      )}
      {line.acts.includes('unpause') && (
        // `actsOn`, not `name`: the row says the source in words and the route
        // is keyed by its identifier. Posting what the row is called would
        // ask the backend to start something with no such name.
        <Hint label={UNPAUSE_SAYS}>
          <Button
            onClick={() => acts.unpause(line.actsOn ?? line.name)}
            ariaLabel={`Unpause ${line.name}`}
          >
            unpause
          </Button>
        </Hint>
      )}
      {line.acts.includes('remove') && (
        // The same call as `unpause`, and a different word, because the word
        // has to name what pressing it does. Nothing files under a deleted
        // source any more, so what this clears is the row: there is nothing
        // to resume and a button saying so would be the panel offering a
        // repair it cannot make.
        <Hint label={REMOVE_SAYS}>
          <Button
            onClick={() => acts.unpause(line.actsOn ?? line.name)}
            ariaLabel={`Remove the record of ${line.name}`}
          >
            remove
          </Button>
        </Hint>
      )}
      {line.acts.includes('cancel') && (
        <Hint label={CANCEL_SAYS}>
          <Button tone="danger" onClick={() => acts.cancel(id)} ariaLabel={`Cancel ${line.name}`}>
            cancel
          </Button>
        </Hint>
      )}
    </RowActions>
  );
}
