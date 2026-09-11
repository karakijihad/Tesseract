// What the runtime did on a day: what it ran, how each call went, and what it
// left behind that anyone can go and check.
//
// Three grains of one view rather than three views. The roster answers the
// question in the operator's own words, what did it use and did it work, and
// stays at thirty rows on a day of nearly three hundred calls. It cannot hold
// an incident together, so the trouble band sits above it: on one measured day
// four of thirteen troubles were a single five minute stretch and the roster
// put them on four separate rows with nothing joining them. The turn list is
// one click away, because the moment something looks wrong the question stops
// being about the tool and starts being about the turn it happened in.
//
// Every figure comes from `/api/conscience/day`, which is the same reader
// `day_read` answers a channel with. Nothing is counted in this file. A second
// arithmetic over the same rows is a second answer waiting to disagree, and
// the operator would have no way to tell which one was lying.

import { useEffect, useMemo, useState } from 'react';

import { Button } from '../../components/common/Button';
import { Fact } from '../../components/common/Fact';
import { Input } from '../../components/common/Input';
import { Meter, type MeterSegment, type MeterTone } from '../../components/common/Meter';
import { Note } from '../../components/common/Note';
import { Row } from '../../components/common/Row';
import { Segmented } from '../../components/common/Segmented';
import { Tabs } from '../../components/common/Tabs';
import { Hint } from '../../components/ui/Hint';
import {
  useConscienceStore,
  type DayCall,
  type DayReport,
  type DayTool,
  type DayTurn,
} from '../../stores/conscience';
import './DayPanel.css';

/** Plain words for every outcome the record can carry, and the order they
 *  stack in a bar: clean first, worst last, so a bar reads left to right the
 *  way the eye already scans.
 *
 *  Written here rather than read from the liveness labels, which answer a
 *  different question. That vocabulary says whether a CAPABILITY is well, and
 *  `caller_error` is correctly quiet there. A row telling the operator a
 *  failed call was "quiet" is worse than showing the raw word. */
const OUTCOMES: readonly { key: string; word: string; tone: MeterTone }[] = [
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
const TONE = new Map(OUTCOMES.map((o) => [o.key, o.tone]));

/** How long a range reaches back when the operator turns one on. Days, and it
 *  is a starting point they then move, not a setting. */
const SPAN_DAYS = 6;

function wordFor(outcome: string): string {
  return WORD.get(outcome) ?? outcome;
}

function shift(day: string, by: number): string {
  const at = new Date(`${day}T00:00:00`);
  at.setDate(at.getDate() + by);
  return at.toISOString().slice(0, 10);
}

function clockOf(at: string): string {
  return new Date(at).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
  });
}

function segmentsOf(byOutcome: Record<string, number>): MeterSegment[] {
  return OUTCOMES.filter((o) => byOutcome[o.key]).map((o) => ({
    value: byOutcome[o.key],
    tone: o.tone,
    label: o.word,
  }));
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
function Reason({ text, fallback }: { text: string; fallback: string }) {
  return (
    <Hint label={text || undefined} maxWidth={520}>
      <span className="day-panel__reason t-meta">{text || fallback}</span>
    </Hint>
  );
}

export function DayPanel() {
  const report = useConscienceStore((s) => s.day);
  const on = useConscienceStore((s) => s.dayOn);
  const through = useConscienceStore((s) => s.dayThrough);
  const loading = useConscienceStore((s) => s.dayLoading);
  const error = useConscienceStore((s) => s.dayError);
  const fetchDay = useConscienceStore((s) => s.fetchDay);

  const [grain, setGrain] = useState<'tools' | 'turns'>('tools');
  const [openTool, setOpenTool] = useState<string | null>(null);
  const [openTurn, setOpenTurn] = useState<string | null>(null);

  useEffect(() => {
    if (!report && !loading && !error) void fetchDay();
  }, [report, loading, error, fetchDay]);

  if (error) {
    return <Note tone="bad">The day could not be read: {error}</Note>;
  }
  if (!report) {
    return <p className="t-meta">Reading what it did.</p>;
  }
  if (!report.anchor) {
    return (
      <Note>
        Nothing has been recorded yet. Records start the first time the app
        takes a turn, and they are kept for thirty days.
      </Note>
    );
  }

  const showing = on ?? report.anchor;
  const reachable = report.available;
  const earliest = reachable[0] ?? showing;
  const latest = reachable[reachable.length - 1] ?? showing;

  return (
    <div className="day-panel">
      <div className="day-panel__controls">
        <Button
          onClick={() => fetchDay(shift(showing, -1), null)}
          disabled={loading || showing <= earliest}
          ariaLabel="the day before"
        >
          ←
        </Button>
        <Input
          type="date"
          value={showing}
          min={earliest}
          max={latest}
          onChange={(next) => next && fetchDay(next, through)}
          ariaLabel="which day to read"
          className="day-panel__date"
        />
        <Button
          onClick={() => fetchDay(shift(showing, 1), null)}
          disabled={loading || showing >= latest}
          ariaLabel="the day after"
        >
          →
        </Button>
        <Segmented
          label="How much to read"
          value={through ? 'range' : 'one'}
          onSelect={(key) =>
            fetchDay(
              key === 'range' ? shift(showing, -SPAN_DAYS) : showing,
              key === 'range' ? showing : null,
            )
          }
          items={[
            { key: 'one', label: 'one day' },
            { key: 'range', label: 'a range' },
          ]}
          className="day-panel__span"
        />
        {through && (
          <Input
            type="date"
            value={through}
            min={showing}
            max={latest}
            onChange={(next) => next && fetchDay(showing, next)}
            ariaLabel="the last day of the range"
            className="day-panel__date"
          />
        )}
        <span className="day-panel__reach t-meta">
          {reachable.length} days of records
        </span>
      </div>

      <Summary report={report} />

      {report.unreadable > 0 && (
        <Note tone="warn">
          {report.unreadable} records could not be read, so nothing below counts
          them. The rest of the day is complete.
        </Note>
      )}

      <Trouble calls={report.trouble} total={report.callCount} />

      <Tabs
        label="How to read the day"
        active={grain}
        onSelect={(key) => setGrain(key)}
        items={[
          { key: 'tools', label: 'By tool', badge: report.tools.length },
          { key: 'turns', label: 'By turn', badge: report.turnCount },
        ]}
      />

      {grain === 'tools' ? (
        <Roster
          tools={report.tools}
          calls={report.calls}
          open={openTool}
          onOpen={setOpenTool}
        />
      ) : (
        <Turns
          turns={report.turns}
          calls={report.calls}
          open={openTurn}
          onOpen={setOpenTurn}
        />
      )}
    </div>
  );
}

/** The day in five figures, and the doors it came through.
 *
 * `unverified` gets a place whether or not it happened, because the reading
 * that matters is often that it is zero. A figure that appears only when it is
 * non-zero cannot be seen to be fine. */
function Summary({ report }: { report: DayReport }) {
  const said = report.byOutcome;
  const doors = report.byDoor
    .map((d) => `${d.turns} from ${d.name}`)
    .join(', ');
  return (
    <div className="day-panel__summary">
      <Fact label="turns">{report.turnCount}</Fact>
      <Fact label="tool calls">{report.callCount}</Fact>
      <Fact label="asked wrong">
        <span className={said.caller_error ? 'day-panel__figure--info' : undefined}>
          {said.caller_error ?? 0}
        </span>
      </Fact>
      <Fact label="nothing to check">
        <span className={said.unverified ? 'day-panel__figure--unverified' : undefined}>
          {said.unverified ?? 0}
        </span>
      </Fact>
      <Fact label="failed">
        <span className={said.failed ? 'day-panel__figure--bad' : undefined}>
          {said.failed ?? 0}
        </span>
      </Fact>
      {doors && <Fact label="came in through">{doors}</Fact>}
    </div>
  );
}

/** Everything that was not a clean success, in the order it happened.
 *
 * Above the roster rather than inside it, because this is the half the roster
 * cannot do: an incident is consecutive in time and scattered across tools,
 * and the rows that matter are the ones you want without scrolling. */
function Trouble({ calls, total }: { calls: DayCall[]; total: number }) {
  if (total === 0) return null;
  if (calls.length === 0) {
    return (
      <Note>
        Every one of the {total} calls came back clean, and each that could
        leave a mark left one.
      </Note>
    );
  }
  return (
    <section className="day-panel__trouble">
      <h3 className="day-panel__heading">
        Not clean
        <span className="t-meta"> · {calls.length} of {total} calls</span>
      </h3>
      {calls.map((call, i) => (
        <div className="day-panel__line" key={`${call.turn}-${call.tool}-${i}`}>
          <span className="day-panel__clock t-meta">{clockOf(call.at)}</span>
          <span className="day-panel__tool">{call.tool}</span>
          <span className={`day-panel__word day-panel__word--${TONE.get(call.outcome) ?? 'quiet'}`}>
            {wordFor(call.outcome)}
          </span>
          <Reason text={call.reason} fallback={`on ${call.door}`} />
        </div>
      ))}
    </section>
  );
}

/** One row per tool: how often, how it went, and how much of it left a mark.
 *
 * The bar is the reading and the count beside it is the same reading in a form
 * the bar cannot give a screen reader, which is why both are here. */
function Roster({
  tools,
  calls,
  open,
  onOpen,
}: {
  tools: DayTool[];
  calls: DayCall[];
  open: string | null;
  onOpen: (tool: string | null) => void;
}) {
  const byTool = useMemo(() => {
    const held = new Map<string, DayCall[]>();
    for (const call of calls) {
      const mine = held.get(call.tool);
      if (mine) mine.push(call);
      else held.set(call.tool, [call]);
    }
    return held;
  }, [calls]);

  if (tools.length === 0) {
    return <Note>Nothing ran on this day.</Note>;
  }

  return (
    <div className="day-panel__rows">
      {tools.map((tool) => {
        const shown = open === tool.tool;
        return (
          <div key={tool.tool}>
            <Row
              className="day-tool"
              onClick={() => onOpen(shown ? null : tool.tool)}
              ariaExpanded={shown}
              ariaLabel={`${tool.tool}, ${tool.calls} calls`}
            >
              <span className="day-tool__name">{tool.tool}</span>
              <span className="day-tool__count">{tool.calls}</span>
              <Meter
                segments={segmentsOf(tool.byOutcome)}
                ariaLabel={`how ${tool.tool} went`}
              />
              <span className="day-tool__marks t-meta">
                {tool.receipts > 0 && (
                  <Hint label="What these calls left behind, that a later pass can go and check.">
                    <span>{tool.receipts} left a mark</span>
                  </Hint>
                )}
              </span>
            </Row>
            {shown && (
              <div className="day-panel__detail">
                {(byTool.get(tool.tool) ?? []).map((call, i) => (
                  <div className="day-panel__line" key={`${call.turn}-${i}`}>
                    <span className="day-panel__clock t-meta">{clockOf(call.at)}</span>
                    <span className={`day-panel__word day-panel__word--${TONE.get(call.outcome) ?? 'quiet'}`}>
                      {wordFor(call.outcome)}
                    </span>
                    <span className="day-panel__clock t-meta">{Math.round(call.ms)} ms</span>
                    <Reason
                      text={
                        call.receipt
                          ? `${call.receipt.kind} ${call.receipt.id}`
                          : call.reason
                      }
                      fallback={`on ${call.door}`}
                    />
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

/** The day as it happened, one row per turn.
 *
 * The door is on the row and it is the one thing the roster cannot show: a
 * failure the operator watched happen and one that happened on their phone
 * overnight read identically per tool. */
function Turns({
  turns,
  calls,
  open,
  onOpen,
}: {
  turns: DayTurn[];
  calls: DayCall[];
  open: string | null;
  onOpen: (turn: string | null) => void;
}) {
  if (turns.length === 0) {
    return <Note>Nothing ran on this day.</Note>;
  }
  return (
    <div className="day-panel__rows">
      {turns.map((turn) => {
        const shown = open === turn.turn;
        return (
          <div key={turn.turn}>
            <Row
              className="day-turn"
              onClick={() => onOpen(shown ? null : turn.turn)}
              ariaExpanded={shown}
              ariaLabel={`${turn.label}, ${clockOf(turn.at)}`}
            >
              <span className="day-panel__clock t-meta">{clockOf(turn.at)}</span>
              <span className="day-turn__door t-meta">{turn.door}</span>
              <span className="day-turn__tools">
                {turn.tools.length === 0 ? (
                  <span className="t-meta">no tools, it just answered</span>
                ) : (
                  turn.tools.map((name) => (
                    <span className="day-turn__chip" key={name}>
                      {name}
                    </span>
                  ))
                )}
              </span>
              <span className={`day-panel__word day-panel__word--${TONE.get(turn.outcome) ?? 'quiet'}`}>
                {wordFor(turn.outcome)}
              </span>
            </Row>
            {shown && (
              <div className="day-panel__detail">
                {calls
                  .filter((c) => c.turn === turn.turn)
                  .map((call, i) => (
                    <div className="day-panel__line" key={`${call.tool}-${i}`}>
                      <span className="day-panel__clock t-meta">{clockOf(call.at)}</span>
                      <span className="day-panel__tool">{call.tool}</span>
                      <span className={`day-panel__word day-panel__word--${TONE.get(call.outcome) ?? 'quiet'}`}>
                        {wordFor(call.outcome)}
                      </span>
                      <Reason
                        text={
                          call.receipt
                            ? `${call.receipt.kind} ${call.receipt.id}`
                            : call.reason
                        }
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
            )}
          </div>
        );
      })}
    </div>
  );
}
