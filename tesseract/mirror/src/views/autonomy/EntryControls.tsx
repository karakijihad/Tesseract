// Changing what an entry does, from the card that describes it.
//
// **The card was readable and not answerable.** It named the cadence, the next
// time and the ceiling, and every one of them could only be changed by opening
// a yaml file on the machine the app is installed on. Away from that machine
// for three days, none of them existed.
//
// **What may be changed is the backend's answer**, on the payload, for the
// reason the managed room already gives: a view that worked it out would draw
// a control the write then refuses. `canSetCadence` is false on a row that
// waits on an event, because a clock is not what fires it. `canSetCeiling`
// wants two things at once: a ceiling already declared under this entry's own
// name, which is the same set `POST /api/settings/cost` will write, and an
// entry that bills to that name rather than to whatever work it dispatches.
// Drawn on either alone, the control offered an edit the route answered with
// `unknown role`, or one that raised the global ceiling while capping nothing.
//
// **Each control writes on the seam that already owns the field.** A cadence
// goes to the scheduler, which persists an override into the operator's own
// `schedule.yaml` field by field, so a cadence corrected in an update still
// reaches whatever they left alone. A ceiling goes to `POST
// /api/settings/cost`, which writes `roles.yaml` and reloads the ledger. There
// is no new writer here and no second answer to either field.

import { useState } from 'react';
import { Button } from '../../components/common/Button';
import { Input } from '../../components/common/Input';
import { Fact } from '../../components/common/Fact';
import { sendCommand } from '../../lib/commands';
import { postCostSettings } from '../../lib/api';
import { useIdentityStore } from '../../stores/identity';
import { useToastStore } from '../../stores/toasts';

/** A cron field that fires once a day: `<minute> <hour> * * *`. The one shape
 *  where "at what time" is a question with an answer, so it is the one shape
 *  that gets a clock rather than an expression to type. */
const DAILY = /^(\d{1,2}) (\d{1,2}) \* \* \*$/;

/** `HH:MM` from a daily cron, or empty for any other cadence. */
export function timeOf(cadence: string): string {
  const at = DAILY.exec(cadence.trim());
  if (at === null) return '';
  const [, minute, hour] = at;
  return `${hour.padStart(2, '0')}:${minute.padStart(2, '0')}`;
}

/** The same cadence, moved to `HH:MM`. Only ever called on one that already
 *  matched, so the rest of the expression is carried through untouched. */
export function cadenceAt(cadence: string, time: string): string {
  const [hour, minute] = time.split(':');
  if (hour === undefined || minute === undefined) return cadence;
  return `${Number(minute)} ${Number(hour)} * * *`;
}

/** What was typed, as a ceiling, or null if it is not one.
 *
 * A pure function so the rule can be read and tested as itself: a number field
 * coerces most of what a person could type before this ever sees it, which
 * makes the interesting cases unreachable through the DOM and easy to leave
 * unheld.
 *
 * **Positive, not merely non-negative.** A ceiling of nothing does not mean
 * uncapped: the ledger stops a spender once what it has spent reaches its cap,
 * and nothing already reaches nothing, so a zero refuses the entry before its
 * first call of the day. The manifest refused the value for this reason until
 * it stopped declaring ceilings at all.
 *
 * **Finite**, because `Number("Infinity")` is not, and a cap that is neither
 * above nor below anything makes every comparison against it, and the global
 * ceiling that sums the caps, indeterminate. The cost route says the same.
 */
export function ceilingFrom(typed: string): number | null {
  const next = Number(typed.trim());
  if (typed.trim() === '' || !Number.isFinite(next) || next <= 0) return null;
  return next;
}

function failed(what: string, err: unknown): void {
  useToastStore
    .getState()
    .push(`${what}: ${err instanceof Error ? err.message : String(err)}`, 'error');
}

/** One fact that can be changed. Reads as the fact until asked, so a card
 *  nobody is editing looks like a card and not like a form. */
function Editable({
  label,
  value,
  children,
  onEdit,
  onSave,
  saveLabel,
  editing,
  onCancel,
}: {
  label: string;
  value: React.ReactNode;
  children: React.ReactNode;
  onEdit: () => void;
  onSave: () => void;
  saveLabel: string;
  editing: boolean;
  onCancel: () => void;
}): React.ReactElement {
  if (!editing) {
    return (
      <Fact label={label}>
        {value}{' '}
        <Button tone="inline" onClick={onEdit} ariaLabel={`Change ${label}`}>
          change
        </Button>
      </Fact>
    );
  }
  return (
    <Fact label={label}>
      <span className="entry-edit">
        <span className="entry-edit__fields">{children}</span>
        <span className="entry-edit__acts">
          <Button tone="primary" onClick={onSave} ariaLabel={saveLabel}>
            save
          </Button>
          <Button onClick={onCancel} ariaLabel="Stop changing it">
            cancel
          </Button>
        </span>
      </span>
    </Fact>
  );
}

/** When it fires, and what it may spend. Both write and then ask the card to
 *  read itself again: what the entry now says is the backend's answer, and
 *  guessing it after a click is how the two come to disagree. */
export function CadenceControl({
  name,
  cadence,
}: {
  name: string;
  cadence: string;
}): React.ReactElement {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(cadence);
  const clock = timeOf(draft);

  const save = () => {
    const next = draft.trim();
    if (next === '' || next === cadence) {
      setEditing(false);
      return;
    }
    // The scheduler answers a command with an envelope and refuses a cadence
    // it cannot parse, so what comes back is its judgement rather than this
    // file's guess at one. Deliberately no re-read here: the envelope is what
    // says the write happened, and the card is watching for it.
    sendCommand('/schedule-set-cadence', ` ${name} ${next}`);
    setEditing(false);
  };

  return (
    <Editable
      label="Cadence"
      value={cadence || 'none set'}
      editing={editing}
      onEdit={() => {
        setDraft(cadence);
        setEditing(true);
      }}
      onCancel={() => setEditing(false)}
      onSave={save}
      saveLabel={`Save the cadence for ${name}`}
    >
      {clock !== '' && (
        // A daily row's time, as a clock. It sets the same field the box
        // beside it holds, so there is one value being edited and not two.
        <Input
          type="time"
          value={clock}
          onChange={(next) => setDraft(cadenceAt(draft, next))}
          ariaLabel={`The time ${name} fires`}
          className="entry-edit__time"
        />
      )}
      <Input
        value={draft}
        onChange={setDraft}
        ariaLabel={`How often ${name} fires`}
        placeholder="0 23 * * * or 15m"
        className="entry-edit__field"
      />
    </Editable>
  );
}

export function CeilingControl({
  name,
  ceiling,
  onWritten,
}: {
  name: string;
  ceiling: number | null;
  onWritten: () => void;
}): React.ReactElement {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(ceiling === null ? '' : ceiling.toFixed(2));

  const save = () => {
    const next = ceilingFrom(draft);
    if (next === null) {
      useToastStore
        .getState()
        .push(
          'A ceiling has to be more than zero. Setting it to nothing stops ' +
            `${name} on its next run rather than leaving it uncapped, so turn ` +
            'it off instead if that is what you want.',
          'error',
        );
      return;
    }
    postCostSettings({ per_role: { [name]: next } })
      .then((res) => {
        // The route answers with the whole cost picture, and Settings, Costs
        // holds the same one. Dropping it left that view seeded from the
        // snapshot it fetched before this edit, and its save posts the entire
        // per role map: changing anything else there would have written the
        // old ceiling back over this one.
        useIdentityStore.getState().setCostTracking(res);
        setEditing(false);
        onWritten();
      })
      .catch((err: unknown) => failed(`Could not change what ${name} may spend`, err));
  };

  return (
    <Editable
      label="Ceiling a day"
      value={ceiling === null ? 'no ceiling of its own' : `$${ceiling.toFixed(2)}`}
      editing={editing}
      onEdit={() => {
        setDraft(ceiling === null ? '' : ceiling.toFixed(2));
        setEditing(true);
      }}
      onCancel={() => setEditing(false)}
      onSave={save}
      saveLabel={`Save what ${name} may spend in a day`}
    >
      <Input
        type="number"
        value={draft}
        onChange={setDraft}
        min={0.01}
        step={0.05}
        ariaLabel={`Dollars ${name} may spend in a day`}
        className="entry-edit__field"
      />
    </Editable>
  );
}
