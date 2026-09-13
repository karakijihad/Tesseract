// One turn's own record: the words for how a call went, and the call-by-call
// body of a single turn.
//
// Lifted out of `DayPanel.tsx`, where every one of these lived private and
// unexported, because a second surface now needs the same rendering. Autonomy
// opens a recovered turn in place rather than sending the operator across to
// this panel, and the thing it opens is the SAME record this file already
// draws: the day reader (`/api/conscience/day`) is the one source for both,
// and this is the one renderer for what a turn did, so the two callers cannot
// come to draw one turn two different ways.
//
// `OUTCOMES`/`TONE`/`wordFor`/`clockOf`/`Reason`/`OutcomeWord` are the
// vocabulary every grain of the day view speaks (the roster, the trouble
// band, the turn list), and they stay here rather than splitting into a
// second file: one word list for what a call's outcome is called, and one
// rendering of it, in one place.

import { Hint } from '../../components/ui/Hint';
import type { MeterTone } from '../../components/common/Meter';
import type { DayCall, DayTurn } from '../../stores/conscience';
import './DayPanel.css';

/** Plain words for every outcome the record can carry, and the order they
 *  stack in a bar: clean first, worst last, so a bar reads left to right the
 *  way the eye already scans.
 *
 *  Written here rather than read from the liveness labels, which answer a
 *  different question. That vocabulary says whether a CAPABILITY is well, and
 *  `caller_error` is correctly quiet there. A row telling the operator a
 *  failed call was "quiet" is worse than showing the raw word. */
export const OUTCOMES: readonly { key: string; word: string; tone: MeterTone }[] = [
  { key: 'succeeded', word: 'worked', tone: 'ok' },
  { key: 'skipped_no_work', word: 'nothing to do', tone: 'quiet' },
  { key: 'caller_error', word: 'asked wrong', tone: 'info' },
  { key: 'unverified', word: 'nothing to check', tone: 'unverified' },
  { key: 'truncated', word: 'ran out of time', tone: 'quiet' },
  { key: 'degraded', word: 'did less than it promises', tone: 'warn' },
  { key: 'refused', word: 'not allowed to start', tone: 'warn' },
  { key: 'skipped_upstream_failed', word: 'what it needed had not worked', tone: 'warn' },
  { key: 'failed', word: 'failed', tone: 'bad' },
];

const WORD = new Map(OUTCOMES.map((o) => [o.key, o.word]));
export const TONE = new Map(OUTCOMES.map((o) => [o.key, o.tone]));

export function wordFor(outcome: string): string {
  return WORD.get(outcome) ?? outcome;
}

export function clockOf(at: string): string {
  return new Date(at).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
  });
}

/** The outcome, coloured by its tone and named in plain words. The one
 *  rendering act for `TONE`/`wordFor`, so every place that shows how a call
 *  or a turn went reaches for this instead of rebuilding the class
 *  expression and the span by hand. */
export function OutcomeWord({ outcome }: { outcome: string }) {
  return (
    <span className={`day-panel__word day-panel__word--${TONE.get(outcome) ?? 'quiet'}`}>
      {wordFor(outcome)}
    </span>
  );
}

/** The runtime's own sentence about a call, which can be a captured stderr
 *  dump hundreds of characters long. The row shows what fits and the whole of
 *  it is one hover away.
 *
 *  `Hint` rather than a `title`, because the browser's tooltip waits a second,
 *  cannot be styled and never appears on touch, and a reason nobody can read
 *  on a phone is the half of this panel that matters most when away from the
 *  desk. A row with no reason renders bare: `Hint` with no label attaches no
 *  listeners, so a clean day costs nothing. */
export function Reason({ text, fallback }: { text: string; fallback: string }) {
  return (
    <Hint label={text || undefined} maxWidth={520}>
      <span className="day-panel__reason t-meta">{text || fallback}</span>
    </Hint>
  );
}

/** One turn's whole body: every call it made and how each one went, and what
 *  it closed if it closed anything. `calls` is the day's full list, the same
 *  shape `DayPanel` already holds; this filters to the one turn itself, so a
 *  caller with no reason to slice the day's calls first does not have to. */
export function TurnRecord({ turn, calls }: { turn: DayTurn; calls: DayCall[] }) {
  const mine = calls.filter((c) => c.turn === turn.turn);
  return (
    <div className="day-panel__detail">
      {mine.map((call, i) => (
        <div className="day-panel__line" key={`${call.tool}-${i}`}>
          <span className="day-panel__clock t-meta">{clockOf(call.at)}</span>
          <span className="day-panel__tool">{call.tool}</span>
          <OutcomeWord outcome={call.outcome} />
          <Reason
            text={call.receipt ? `${call.receipt.kind} ${call.receipt.id}` : call.reason}
            fallback=""
          />
        </div>
      ))}
      {turn.taskOutcome && (
        <p className="day-panel__task t-meta">
          It closed a task as {turn.taskOutcome}, decided by{' '}
          {turn.taskVerifiedBy === 'gate'
            ? "the project's own checks"
            : 'its own account of the work'}
          .
        </p>
      )}
    </div>
  );
}
