// The operator journal. What the kernel decided, and when.
//
// One band of state lines, like every other room. It drew bordered cards with
// a chip in the head of each before, which is the shape the panel replaced
// everywhere else.
//
// The only thing this file authors is the word for an event kind, which names
// a category rather than describing the machine.

import { Note } from '../../components/common/Note';
import type { OperatorJournalRow } from '../../lib/api';
import { Markdown } from '../../components/common/Markdown';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
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

function toLine(row: OperatorJournalRow, index: number): StateLine {
  const kind = EVENT_LABEL[row.event_type] ?? row.event_type;
  return {
    key: `${row.ts}:${row.event_type}:${row.agenda_item_id ?? row.worker_id ?? index}`,
    state: HELD_BACK.has(row.event_type) ? 'pending' : 'idle',
    label: kind,
    name: kind,
    said: detail(row),
    when: clock(row.ts),
    value: row.agenda_item_id ?? row.worker_id ?? '',
  };
}

export function JournalPane({
  rows,
  status,
  error,
}: {
  rows: OperatorJournalRow[];
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
}): React.ReactElement {
  if (status === 'error') {
    return <Note tone="bad">The journal could not be read. {error}</Note>;
  }
  if (status !== 'ready') return <></>;
  if (rows.length === 0) {
    return (
      <p className="t-meta">
        Nothing yet. What the kernel approves, dispatches and decides lands here
        as it happens.
      </p>
    );
  }

  return (
    <div className="autonomy-group" data-testid="autonomy-journal-pane">
      <Band label="What it decided" count={rows.length} />
      <StateStrip lines={rows.map(toLine)} />
    </div>
  );
}
