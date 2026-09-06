// What each room just did, in its own record lines.
//
// **The SENTENCE is not here.** `GET /api/autonomy/rooms` writes it, from the
// numbers the backend counts, under the rule the watchman already follows: a
// figure that was not observed is dropped and the counts are published
// instead. This file never writes a word about the machine.
//
// **The MARK is not here either, any more.** It was, in eight functions, one
// per room, and they disagreed: `failed` rendered red in six of them and amber
// in two, `refused` amber in one and red in another, and the room that counted
// `not_instrumented` as bad drew a red rail dot over a neutral dashed row on
// the same screen. Each author had solved their own case, and each had left a
// comment warning about exactly that hazard. It is one function on the backend
// now, `routes/autonomy_rooms.py::marks_for`, over one vocabulary,
// `orchestrator/obligation.py`, and it arrives beside the room's sentence.
//
// What is left here is the TAIL: that room's own record lines, in the order
// they happened. Not prose, and not a severity.
//
// **The invariant list this panel is built against** is in
// `routes/autonomy_health.py`. The three that bind this file: colour encodes
// obligation and never internal state; severity is decided once, in one
// function, from one vocabulary; and never two answers to one question on one
// screen.

import type {
  AgendaItem,
  OperationalState,
  AtlasResponse,
  ChannelsResponse,
  MemoryResponse,
  GovernorPause,
  HealthResponse,
  ManagedResponse,
  OperatorJournalRow,
  OverviewResponse,
  PrunedResponse,
  RetentionResponse,
} from '../../lib/api';
import type { RoomTail, TailLine } from './RoomShell';

/** How many lines the bottom band carries. It is evidence under the room, not
 *  a feed competing with it. */
const TAIL_LINES = 4;

/** Statuses that mean the item is waiting on something outside it. */
const HELD: ReadonlySet<string> = new Set(['blocked', 'awaiting_operator']);

export interface RoomLine {
  tail: RoomTail;
}

/** What a room whose feed could not be read says under it. The rail's own mark
 *  comes from the backend and is simply absent when this is: a rail that
 *  invented a colour for a room it could not read would be the ninth roll-up. */
const NO_RECORD = 'Nothing records this room yet.';

function unwired(label: string): RoomLine {
  return { tail: { label, lines: [], unwired: NO_RECORD } };
}

function clock(iso: string | null | undefined): string {
  if (!iso) return '';
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return '';
  const now = new Date();
  if (at.toDateString() === now.toDateString()) return at.toTimeString().slice(0, 8);
  return at.toLocaleDateString(undefined, { day: 'numeric', month: 'short' });
}

/** How a record line is toned.
 *
 * ONE function for every room's tail, over one vocabulary, which is what the
 * eight roll-ups were not. It reads the STATE rather than the obligation, and
 * that is the distinction the two axes exist for: the band above says what
 * wants the operator, and this band says what happened. A step that failed
 * last night asks for nothing and is still the line worth finding on a list of
 * twenty.
 */
function tone(state: OperationalState): TailLine['severity'] {
  if (state === 'failed') return 'bad';
  if (state === 'degraded' || state === 'refused') return 'warn';
  return 'info';
}

function newestFirst(lines: TailLine[]): TailLine[] {
  return [...lines]
    .sort((a, b) => (a.sortKey < b.sortKey ? 1 : a.sortKey > b.sortKey ? -1 : 0))
    .slice(0, TAIL_LINES);
}

interface RoomInputs {
  overview: OverviewResponse | null;
  items: AgendaItem[];
  pauses: GovernorPause[];
  journal: OperatorJournalRow[];
  health: HealthResponse | null;
  managed: ManagedResponse | null;
  pruned: PrunedResponse | null;
  channels: ChannelsResponse | null;
  memory: MemoryResponse | null;
  atlas: AtlasResponse | null;
  retention: RetentionResponse | null;
}

/** Overview: what wants the operator, and what the machine just did.
 *
 * The tail is deliberately NOT the first band restated. The band is what needs you;
 * the line under it is what happened, whether or not anything went wrong. */
function overviewRoom({ overview }: RoomInputs): RoomLine {
  const label = 'What just ran';
  if (overview === null) {
    return { tail: { label, lines: [], unwired: NO_RECORD } };
  }
  const lines: TailLine[] = overview.ranWhileAway.map((run) => ({
    key: `ran:${run.name}:${run.at ?? ''}`,
    sortKey: run.at ?? '',
    at: clock(run.at),
    source: run.name,
    text: run.said,
    severity: tone(run.state),
  }));

  return { tail: { label, lines: newestFirst(lines) } };
}

/** Blocked and paused: what is waiting on something outside it. */
function blocked({ items, pauses }: RoomInputs): RoomLine {
  const held = items.filter((i) => HELD.has(i.status));
  const lines: TailLine[] = [
    ...pauses.map((p) => ({
      key: `pause:${p.source}`,
      sortKey: p.paused_at,
      at: clock(p.paused_at),
      source: p.detector,
      text: p.reason,
      severity: 'warn' as const,
    })),
    ...held.map((i) => ({
      key: `held:${i.id}`,
      sortKey: i.updated_at,
      at: clock(i.updated_at),
      source: i.source,
      text: i.blocked_reason || i.goal,
      severity: 'warn' as const,
    })),
  ];
  return {
    tail: { label: 'What is holding them', lines: newestFirst(lines) },
  };
}

/** Recent outcomes: what finished, and what the kernel decided on its own. */
function outcomes({ items }: RoomInputs): RoomLine {
  const lines: TailLine[] = items
    .map((i): TailLine | null => {
      const last = i.status_history[i.status_history.length - 1];
      if (!last) return null;
      return {
        key: `${i.id}:${last.at}`,
        sortKey: last.at,
        at: clock(last.at),
        source: last.by,
        text: last.reason || `${i.goal} is ${last.to_status}`,
        severity:
          last.to_status === 'abandoned' || last.to_status === 'failed' ? 'warn' : 'info',
      };
    })
    .filter((l): l is TailLine => l !== null);
  return {
    tail: { label: 'What it decided', lines: newestFirst(lines) },
  };
}

/** The operator journal. */
function journalRoom({ journal }: RoomInputs): RoomLine {
  const lines: TailLine[] = journal.map((row, i) => ({
    key: `${row.ts}:${i}`,
    sortKey: row.ts,
    at: clock(row.ts),
    source: row.event_type,
    text: row.summary ?? '',
    severity: 'info' as const,
  }));
  return { tail: { label: 'Latest', lines: newestFirst(lines) } };
}

/** Health: is the runtime itself well.
 *
 * The mark is the worst thing in the room, and it comes from states the
 * backend decided. A sweep that has never run is unwired rather than calm:
 * nothing has looked, which is not the same as nothing being wrong. */
function health({ health: feed }: RoomInputs): RoomLine {
  const label = 'The runtime, as it happens';
  if (feed === null) {
    return { tail: { label, lines: [], unwired: NO_RECORD } };
  }
  // A gap and a stated absence are different things, and only one of them is
  // an alarm. `unknown` means something is watching and could not report, so
  // the room is not well; `not_instrumented` means nothing produces this at
  // all, which is a fact the room states and not a fault it has. Counting the
  // second as a warning would leave this rail permanently amber on a machine
  // that has simply never tripped a breaker.
  const lines: TailLine[] = feed.tail.map((row, i) => ({
    key: `${row.sortKey}:${i}`,
    sortKey: row.sortKey,
    at: clock(row.at),
    source: row.source,
    text: row.text,
    severity: row.severity,
  }));
  return {
    tail: { label, lines: newestFirst(lines) },
  };
}

/** Managed system: what the app runs for you, and what you added to it.
 *
 * A row the operator turned off is deliberately not a warning. It reads quiet
 * because nothing is expected of it, and a rail that went amber every time
 * somebody turned something off would teach them to stop reading the colour.
 * What DOES want them is a row that stopped itself and an agent waiting to be
 * approved. */
function managed({ managed: roster }: RoomInputs): RoomLine {
  const label = 'What fired recently';
  if (roster === null) {
    return { tail: { label, lines: [], unwired: NO_RECORD } };
  }
  // Deliberately not the alarms: every pending alarm is `pending`, which is
  // what an alarm IS, so counting them here turned the rail amber for as long
  // as one existed. An agent waiting to be approved is the pending that wants
  // somebody.
  const lines: TailLine[] = roster.schedules
    .filter((l) => l.at)
    .map((l) => ({
      key: `managed:${l.name}`,
      sortKey: l.at ?? '',
      at: clock(l.at),
      source: l.name,
      text: l.said,
      severity: tone(l.state),
    }));
  return { tail: { label, lines: newestFirst(lines) } };
}

/** What was dropped at the door, and why. */
function pruned({ pruned: ledger }: RoomInputs): RoomLine {
  const label = 'Dropped at the door';
  if (ledger === null) {
    return { tail: { label, lines: [], unwired: NO_RECORD } };
  }
  const lines: TailLine[] = ledger.records.map((row, i) => ({
    key: `${row.ts}:${i}`,
    sortKey: row.ts,
    at: clock(row.ts),
    source: row.source,
    text: row.reason || row.goal,
    severity: 'info' as const,
  }));
  // Always quiet, however many were dropped. A draft turned away at the door
  // is the admission gate working, not a fault, and a rail that went amber
  // every time it worked would teach the operator to stop reading the colour.
  // The written line says the count for anyone who wants it.
  return { tail: { label, lines: newestFirst(lines) } };
}

/** Channels: whether the operator is reachable, and what last reached them.
 *
 * The mark is the DOOR's, not the kinds': a muted kind is a choice and a
 * capped one is a pause, while a bridge that is down is the only thing in this
 * room that means the operator is not being told anything at all.
 */
function channels({ channels: payload }: RoomInputs): RoomLine {
  const label = 'What it sent you';
  if (payload === null) return unwired(label);
  const lines: TailLine[] = payload.lastMessage
    ? [
        {
          key: `sent:${payload.lastMessage.at ?? ''}`,
          sortKey: payload.lastMessage.at ?? '',
          at: clock(payload.lastMessage.at),
          source: payload.lastMessage.category,
          text: payload.lastMessage.text,
          severity: 'info' as const,
        },
      ]
    : [];
  return {
    tail: { label, lines },
  };
}

/** Memory: whether the library can be searched, and what last touched it.
 *
 * The mark is RETRIEVAL's. How much is in the library is not a state, and a
 * step that found nothing to do is the commonest outcome of a quiet night; a
 * search that has fallen back to keywords is the one thing in this room the
 * operator would want to know without opening it.
 */
function memory({ memory: payload }: RoomInputs): RoomLine {
  const label = 'What the last pass did to it';
  if (payload === null) return unwired(label);
  const lines: TailLine[] = payload.lastNight.map((step) => ({
    key: step.stage,
    sortKey: step.stage,
    at: step.took,
    source: step.stage,
    text: step.reason || step.label,
    severity: tone(step.state),
  }));
  return {
    tail: { label, lines },
  };
}

/** What it throws away: what ages, and what nothing has decided about.
 *
 * What IS a fault is a tree the sweep could not touch. Nothing swept at all is
 * unwired rather than calm: no window has been applied here, which is not the
 * same as nothing being thrown away.
 */
function retention({ retention: payload }: RoomInputs): RoomLine {
  const label = 'What the last sweep did';
  if (payload === null) return unwired(label);
  const lines: TailLine[] = payload.ages.map((row) => ({
    key: `ages:${row.name}`,
    sortKey: row.name,
    at: clock(row.at),
    source: row.name,
    text: row.said,
    severity: tone(row.state),
  }));
  return {
    tail: { label, lines: newestFirst(lines) },
  };
}

/** Atlas: whether the map of how everything connects can be trusted, and what
 *  the last pass over it did.
 *
 * The mark is the MAP's own, and only what the map says is wrong counts. What
 * it does not cover is a declared gap rather than a fault, and it is the same
 * gap every night: a rail that went amber for it would be permanently amber on
 * a machine where nothing is wrong, which teaches the operator to stop reading
 * the colour. A map nothing has drawn is unwired, because nothing has looked.
 */
function atlas({ atlas: payload }: RoomInputs): RoomLine {
  const label = 'The last pass over it';
  if (payload === null) return unwired(label);
  // `graph` only: the band saying what the map does not cover is every row
  // `not_instrumented` by construction, and reading it here would leave the
  // rail permanently unwired on a machine where nothing is wrong.
  const lines: TailLine[] = payload.lastPass.map((step) => ({
    key: step.stage,
    sortKey: step.stage,
    at: step.took,
    source: step.stage,
    text: step.reason || step.label,
    severity: tone(step.state),
  }));
  return {
    tail: { label, lines },
  };
}

export type RoomKey =
  | 'overview'
  | 'blocked'
  | 'health'
  | 'managed'
  | 'memory'
  | 'outcomes'
  | 'journal'
  | 'pruned'
  | 'channels'
  | 'retention'
  | 'atlas';

export function roomLines(inputs: RoomInputs): Record<RoomKey, RoomLine> {
  return {
    overview: overviewRoom(inputs),
    blocked: blocked(inputs),
    health: health(inputs),
    managed: managed(inputs),
    memory: memory(inputs),
    outcomes: outcomes(inputs),
    journal: journalRoom(inputs),
    pruned: pruned(inputs),
    // What it sent you, from the notifier's own record. It was `unwired` until
    // that record existed, which is the honest answer when nothing is
    // watching and the wrong one once something is.
    channels: channels(inputs),
    retention: retention(inputs),
    atlas: atlas(inputs),
  };
}
