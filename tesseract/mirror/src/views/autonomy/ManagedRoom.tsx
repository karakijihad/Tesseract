// Managed system. Everything the app runs for you, and everything you added.
//
// **Ownership is the only axis here.** What the app ships is the app's to
// change on an update; what the operator wrote is theirs forever, and that is
// the difference that decides what a control may offer. Whether a row may be
// deleted is the backend's answer, on the row, because a view that decided it
// would draw a control the route then refuses.
//
// **This file authors nothing about any of them.** Every state, its words, and
// the sentence beside each name arrive in the payload. What is written here is
// the field the control acts on: `run now`, `disable`, `delete`. Those name a
// control rather than describing a job.
//
// It replaces the Schedule and the Agents tabs, which are deleted. A tab that
// existed only to be linked into is the duplication that came back through the
// side door once already.

import { useEffect, useState } from 'react';
import { Button } from '../../components/common/Button';
import { Note } from '../../components/common/Note';
import { RowActions } from '../../components/common/Row';
import { Segmented } from '../../components/common/Segmented';
import { sendCommand } from '../../lib/commands';
import { deleteScheduleJob, toggleAgentDisabled, cancelAlarm } from '../../lib/api';
import type { ManagedLine, ManagedPlaybook, ManagedResponse } from '../../lib/api';
import { useAutonomyStore } from '../../stores/autonomy';
import { useToastStore } from '../../stores/toasts';
import { AddJobForm } from '../schedule/AddJobForm';
import { AddAlarmForm } from '../schedule/AddAlarmForm';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
import { clock } from '../../lib/time';

// The scheduler broadcasts what it does and `stores/dispatch/schedule.ts`
// re-reads this room on one, so the poll is the backstop rather than the
// mechanism: an agent card edited on disk and a job another surface changed
// arrive on nobody's envelope.
const POLL_MS = 90_000;

type Tab = 'schedules' | 'agents' | 'alarms' | 'playbooks';

const TABS: { key: Tab; label: string }[] = [
  { key: 'schedules', label: 'Schedules' },
  { key: 'agents', label: 'Agents' },
  { key: 'alarms', label: 'Alarms' },
  { key: 'playbooks', label: 'Playbooks' },
];

/** What acting on a row does. Every one of them is a write on the route that
 *  already owns it, and what the row says afterwards is re-read rather than
 *  decided here. */
export interface Acts {
  run: (name: string) => void;
  toggle: (line: ManagedLine, kind: Tab) => void;
  remove: (line: ManagedLine, kind: Tab) => void;
  open: (line: ManagedLine) => void;
  /** Read the roster again. What a form calls when it has changed one. */
  refresh: () => void;
}

function Controls({
  line,
  kind,
  acts,
  read,
}: {
  line: ManagedLine;
  kind: Tab;
  acts: Acts;
  /** When the roster this row came from was read. Any completed re-read
   *  clears the pressed state, including one where nothing about this row
   *  moved: a refused command changes no row, and without this the control
   *  sat reading `starting` until the operator changed room. */
  read: string;
}): React.ReactElement {
  // `canRun` is the backend's answer from the LAST payload, so it is still
  // true across the round trip of a first press. The engine refuses a second
  // hand-fired run now, but a control that stays pressable while nothing on
  // screen changes reads as one that did nothing, and asking twice should not
  // be the way to find that out.
  const [fired, setFired] = useState(false);
  useEffect(() => setFired(false), [line.state, line.at, read]);
  // `RowActions` rather than a span of buttons: it is what stops a click on
  // `run now` from also being a click on the row, which opened the entry card
  // behind every action until it did.
  return (
    <RowActions className="state-acts">
      {line.canRun && (
        <Button
          onClick={() => {
            setFired(true);
            acts.run(line.name);
          }}
          disabled={fired}
          ariaLabel={`Run ${line.name} now`}
        >
          {fired ? 'starting' : 'run now'}
        </Button>
      )}
      {line.canToggle && (
        <Button
          onClick={() => acts.toggle(line, kind)}
          ariaLabel={`${line.enabled ? 'Turn off' : 'Turn on'} ${line.name}`}
        >
          {line.enabled ? 'turn off' : 'turn on'}
        </Button>
      )}
      {line.canDelete && (
        <Button
          tone="danger"
          onClick={() => acts.remove(line, kind)}
          ariaLabel={`Delete ${line.name}`}
        >
          delete
        </Button>
      )}
    </RowActions>
  );
}

function toLine(line: ManagedLine, kind: Tab, acts: Acts, read: string): StateLine {
  const acting = line.canRun || line.canToggle || line.canDelete;
  return {
    key: `${kind}:${line.origin}:${line.name}`,
    state: line.state,
    label: line.label,
    name: line.name,
    said: line.said,
    when: clock(line.at),
    value: line.value,
    // Who wrote it, on the row. It was two headings until the operator found
    // their own row under one and asked for a tag anyway: a heading answers
    // the question only while the row is under it, and the answer has to
    // survive the row being read on its own. Two sections AND a tag would be
    // two mechanisms answering one question, so the sections went.
    tag: line.tag,
    // Only what the payload gives somewhere to go. A row that cannot be
    // opened must not look like it can.
    onOpen: line.opens ? () => acts.open(line) : undefined,
    actions: acting ? (
      <Controls line={line} kind={kind} acts={acts} read={read} />
    ) : undefined,
  };
}

/** A playbook, read into the same one-line shape every roster on this room
 *  uses. It carries no controls: nothing on this panel runs, toggles or
 *  deletes a playbook, so unlike `toLine` this never reaches for `Controls`.
 *
 *  The sentence is the producer's own: what the playbook is for, or, when it
 *  cannot run, the one sentence saying why. `useWhen` rides under the row
 *  rather than beside it, because it is a second sentence and the strip's
 *  `more` slot is what that is for. */
function toPlaybookLine(playbook: ManagedPlaybook): StateLine {
  const value = playbook.version
    ? `v${playbook.version}, ${playbook.status}`
    : playbook.status;
  return {
    key: `playbooks:${playbook.name}`,
    // The state is the backend's, said with the row, like every other roster.
    state: playbook.state,
    name: playbook.name,
    said: playbook.cannotRun || playbook.description,
    value,
    more: playbook.useWhen ? (
      <span className="t-meta">{playbook.useWhen}</span>
    ) : undefined,
  };
}

export function ManagedRoomView({
  managed,
  status,
  error,
  acts,
  initial = 'schedules',
}: {
  managed: ManagedResponse | null;
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
  acts: Acts;
  /** Which roster is showing. Only a test names one: the room opens on the
   *  schedules because that is the half the operator came for. */
  initial?: Tab;
}): React.ReactElement {
  const [tab, setTab] = useState<Tab>(initial);
  const [adding, setAdding] = useState(false);

  if (status === 'error') {
    return (
      <Note tone="bad">What runs on this machine could not be read. {error}</Note>
    );
  }
  if (status !== 'ready' || managed === null) return <></>;

  const lines = tab === 'playbooks' ? [] : managed[tab];
  return (
    <>
      <div className="managed-head">
        <Segmented
          items={TABS}
          value={tab}
          onSelect={(next) => {
            setTab(next);
            setAdding(false);
          }}
          label="What to manage"
        />
        {tab !== 'agents' && tab !== 'playbooks' && (
          <Button
            onClick={() => setAdding((open) => !open)}
            ariaExpanded={adding}
            ariaLabel={adding ? 'close the form' : `add ${tab === 'schedules' ? 'a job' : 'an alarm'}`}
          >
            {adding ? 'cancel' : tab === 'schedules' ? 'new job' : 'new alarm'}
          </Button>
        )}
      </div>

      {adding && tab === 'schedules' && (
        <AddJobForm
          onClose={() => {
            setAdding(false);
            acts.refresh();
          }}
        />
      )}
      {adding && tab === 'alarms' && (
        <AddAlarmForm
          onClose={() => {
            setAdding(false);
            acts.refresh();
          }}
        />
      )}

      {tab === 'playbooks' ? (
        managed.playbooks.length === 0 ? (
          <p className="t-meta">
            No playbooks yet. The assistant writes one down after a way of
            doing something has worked.
          </p>
        ) : (
          <div className="autonomy-group">
            <StateStrip lines={managed.playbooks.map(toPlaybookLine)} />
          </div>
        )
      ) : lines.length === 0 ? (
        <p className="t-meta">
          {tab === 'alarms'
            ? 'No alarm is waiting to go off.'
            : 'Nothing here yet.'}
        </p>
      ) : tab === 'alarms' ? (
        <div className="autonomy-group">
          <Band label="Waiting to go off" count={lines.length} />
          <StateStrip lines={lines.map((l) => toLine(l, tab, acts, managed.observedAt))} />
        </div>
      ) : (
        // One list, the app's own rows first, each carrying its own tag. The
        // order is the route's: it reads the shipped half before yours, so
        // the marks still fall in blocks without this file sorting anything.
        <div className="autonomy-group">
          <StateStrip
            lines={lines.map((l) => toLine(l, tab, acts, managed.observedAt))}
          />
        </div>
      )}

      {tab === 'agents' && (
        // Said out loud rather than left as a missing button. Nothing in the
        // runtime promotes a quarantined card, so a control here would be a
        // claim, and a claim about a control is worse than its absence.
        <Note>
          An agent is created by asking the assistant for one, which puts the
          card in front of you to approve before anything can use it.
        </Note>
      )}
    </>
  );
}

export function ManagedRoom(): React.ReactElement {
  const managed = useAutonomyStore((s) => s.managed);
  const reread = useAutonomyStore((s) => s.rereadManagedRoom);
  const pushLevel = useAutonomyStore((s) => s.pushLevel);

  useEffect(() => {
    void reread();
    const timer = window.setInterval(() => void reread(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [reread]);

  // Every action is a write on the route that already owns it, followed by a
  // re-read. Nothing here decides what the row now says: that is the
  // backend's answer and guessing it after a click is how the two disagree.
  const after = () => {
    // `POST /api/schedule/create` and the delete route answer directly and
    // send no envelope, so the socket path never fires for a write made here.
    void reread();
  };
  const failed = (what: string, err: unknown) =>
    useToastStore
      .getState()
      .push(`${what}: ${err instanceof Error ? err.message : String(err)}`, 'error');

  const acts: Acts = {
    // The scheduler answers a command with an envelope, and the dispatcher
    // re-reads this room when one arrives. A timer here would be a guess at
    // how long the run takes, on the one panel that exists to stop guessing.
    run: (name) => sendCommand('/schedule-run-now', ` ${name}`),
    toggle: (line, kind) => {
      if (kind === 'agents') {
        toggleAgentDisabled(line.name, line.enabled)
          .then(after)
          .catch((err: unknown) =>
            failed(`Could not turn ${line.name} ${line.enabled ? 'off' : 'on'}`, err),
          );
        return;
      }
      sendCommand(line.enabled ? '/schedule-disable' : '/schedule-enable', ` ${line.name}`);
    },
    remove: (line, kind) => {
      const removing =
        kind === 'alarms' ? cancelAlarm(line.name) : deleteScheduleJob(line.name);
      removing
        .then(() => {
          useToastStore.getState().push(`Removed ${line.name}`);
          after();
        })
        .catch((err: unknown) => failed(`Could not remove ${line.name}`, err));
    },
    refresh: after,
    open: (line) => {
      if (!line.opens) return;
      pushLevel({
        kind: line.opens.kind as 'entry' | 'agent',
        id: line.opens.id,
        label: line.name,
      });
    },
  };

  return (
    <ManagedRoomView
      managed={managed.data}
      status={managed.status}
      error={managed.error}
      acts={acts}
    />
  );
}
