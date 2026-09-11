// The operator journal. What the kernel decided, and when.
//
// One band of state lines, like every other room. It drew bordered cards with
// a chip in the head of each before, which is the shape the panel replaced
// everywhere else.
//
// The only thing this file authors is the word for an event kind, which names
// a category rather than describing the machine.

import { Note } from '../../components/common/Note';
import type { OperatorJournalRow, ReturnNoteResponse } from '../../lib/api';
import { Markdown } from '../../components/common/Markdown';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
import { useAutonomyStore, type AutonomyLevel } from '../../stores/autonomy';
import { clock } from '../../lib/time';

// What each kind is called on screen. The enum's slugs are file-tree names; a
// person reading a room needs the act itself.
const EVENT_LABEL: Record<string, string> = {
  approval: 'approved',
  dispatch: 'dispatched',
  outcome: 'outcome',
  advice_only: 'advice only',
  follow_up_draft: 'follow-up',
};

// Which of them is a decision that did NOT go ahead. Everything else is the
// kernel doing what it said it would, which is not a fault and must not draw
// like one.
const HELD_BACK = new Set(['advice_only', 'follow_up_draft']);

/** What a row's own record is, when it names one that can be opened. The item
 *  wins over the worker where a row carries both, because the item is what
 *  the row is really about; a row with neither (which none write today) has
 *  nowhere to go. */
function target(row: OperatorJournalRow): AutonomyLevel | null {
  if (row.agenda_item_id) {
    return { kind: 'agenda', id: row.agenda_item_id, label: row.summary ?? row.agenda_item_id };
  }
  if (row.worker_id) {
    return { kind: 'worker', id: row.worker_id, label: row.summary ?? row.worker_id };
  }
  return null;
}

function detail(row: OperatorJournalRow): React.ReactNode {
  // The summary is model prose. It arrives with markdown in it and used to
  // reach the DOM as a bare text node, asterisks and all.
  if (row.summary) return <Markdown variant="inline">{row.summary}</Markdown>;
  if (row.event_type === 'dispatch' && row.worker_id) return `worker ${row.worker_id}`;
  if (row.event_type === 'outcome' && row.worker_id) {
    const status = typeof row.status === 'string' ? row.status : 'terminal';
    return `worker ${row.worker_id} became ${status}`;
  }
  return row.agenda_item_id ?? '';
}

function toLine(
  row: OperatorJournalRow,
  index: number,
  pushLevel: (level: AutonomyLevel) => void,
): StateLine {
  const kind = EVENT_LABEL[row.event_type] ?? row.event_type;
  const opens = target(row);
  return {
    key: `${row.ts}:${row.event_type}:${row.agenda_item_id ?? row.worker_id ?? index}`,
    state: HELD_BACK.has(row.event_type) ? 'pending' : 'idle',
    label: kind,
    name: kind,
    said: detail(row),
    when: clock(row.ts),
    value: row.agenda_item_id ?? row.worker_id ?? '',
    onOpen: opens ? () => pushLevel(opens) : undefined,
  };
}

export function JournalPane({
  rows,
  status,
  error,
  note,
  noteStatus,
  noteError,
}: {
  rows: OperatorJournalRow[];
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
  note: ReturnNoteResponse | null;
  noteStatus: 'idle' | 'loading' | 'ready' | 'error';
  noteError: string | null;
}): React.ReactElement {
  const pushLevel = useAutonomyStore((s) => s.pushLevel);
  if (status === 'error') {
    return <Note tone="bad">The journal could not be read. {error}</Note>;
  }
  if (status !== 'ready') return <></>;
  // A note that failed to load and a note with nothing in it drew the same
  // way, which is the one thing this band must not do: the operator would
  // read "nothing happened while you were away" off a request that never
  // arrived.
  const noteFailed = noteStatus === 'error';
  // The note is fetched beside the journal rather than after it, so on a quiet
  // day the rows can arrive first and land here while the note is still in
  // flight. Saying "nothing yet" then is the same lie the error case was.
  const noteWaiting = noteStatus === 'idle' || noteStatus === 'loading';
  if (rows.length === 0 && !note?.text && !noteFailed && !noteWaiting) {
    return (
      <p className="t-meta">
        Nothing yet. What the kernel approves, dispatches and decides lands here
        as it happens.
      </p>
    );
  }

  return (
    <div className="autonomy-group" data-testid="autonomy-journal-pane">
      {noteFailed ? (
        <Note tone="bad">
          What changed while you were away could not be read, so this is not a
          quiet absence. {noteError}
        </Note>
      ) : null}
      {noteWaiting && !note?.text ? (
        <p className="t-meta">Reading what changed while you were away.</p>
      ) : null}
      {note?.text ? (
        // The same text the brief carries and the tool returns, rendered
        // rather than restated: a second reader over these records would be
        // a second answer to what happened while nobody was watching.
        <>
          <Band label="While you were away" />
          <Markdown>{note.text}</Markdown>
        </>
      ) : null}
      {rows.length > 0 ? (
        <>
          <Band label="What it decided" count={rows.length} />
          <StateStrip lines={rows.map((row, i) => toLine(row, i, pushLevel))} />
        </>
      ) : null}
    </div>
  );
}
