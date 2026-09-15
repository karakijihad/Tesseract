// The day. What it decided to work on this morning, what it has carried
// forward since, and what that has cost.
//
// AR-29 item 7 draws this so `autonomy_read` can relay it to a phone in the
// same sentences (ruling 23). That is the whole reason it is a room and not a
// digest: the operator away from the desk is told what the operator at it is
// told, from one reader, rather than a second summary written for a channel.
//
// **This file authors nothing about any wake.** `MorningJob` and `WorkdayJob`
// each write a sentence for every way they stop, and the payload carries it.
// A view that re-described a refused wake would be a second account of one
// run, and the two would disagree the first time a stop was reworded.
//
// The one thing it does decide is ORDER: the money first when it could not be
// read, because that is why nothing ran and a reader hunting for the reason
// should not have to pass the wakes to find it.

import { useEffect } from 'react';
import { Button } from '../../components/common/Button';
import { Note } from '../../components/common/Note';
import { RowActions } from '../../components/common/Row';
import { Band } from '../../components/common/StateStrip';
import { Hint } from '../../components/ui/Hint';
import { clock } from '../../lib/time';
import { useAutonomyStore } from '../../stores/autonomy';
import type { DayResponse, TaskVerdict } from '../../lib/api';

/** What pressing each key says, read into a `Hint` beside it because none of
 *  the three words alone says what happens next or that it can be changed. */
const VERDICT_SAYS: Record<TaskVerdict, string> = {
  good: 'Counts this task as work you wanted. Press another to change it.',
  bad: 'Counts this task as work you did not want. Press another to change it.',
  unused: 'Counts this task as work that went unused. Press another to change it.',
};

const VERDICT_LABEL: Record<TaskVerdict, string> = {
  good: 'Good',
  bad: 'Bad',
  unused: 'Not used',
};

const VERDICTS: TaskVerdict[] = ['good', 'bad', 'unused'];

function VerdictKeys({
  taskId,
  said,
  pending,
  onPick,
}: {
  taskId: string;
  said: TaskVerdict | '';
  pending: boolean;
  onPick: (taskId: string, verdict: TaskVerdict) => void;
}): React.ReactElement {
  return (
    <RowActions>
      {VERDICTS.map((verdict) => (
        <Hint key={verdict} label={VERDICT_SAYS[verdict]}>
          <Button
            tone={verdict === 'good' ? 'good' : verdict === 'bad' ? 'danger' : 'default'}
            active={said === verdict}
            disabled={pending}
            onClick={() => onPick(taskId, verdict)}
            ariaLabel={`${VERDICT_LABEL[verdict]} for this task`}
          >
            {VERDICT_LABEL[verdict]}
          </Button>
        </Hint>
      ))}
    </RowActions>
  );
}

/** What each row is called where a person reads it. The runtime's own names
 *  are `morning` and `workday`; neither is a sentence. */
const ROW_LABEL: Record<string, string> = {
  morning: 'Decided the day',
  workday: 'Carried it forward',
};

export function DayRoomView({
  data,
  status,
  error,
  pendingIds,
  onVerdict,
}: {
  data: DayResponse | null;
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
  /** Task ids with a key in flight, so the row's buttons wait for the
   *  answer rather than looking like nothing happened. Defaults to none,
   *  for callers (and tests) that never press one. */
  pendingIds?: Set<string>;
  /** Absent renders the "Finished today" rows with no buttons at all,
   *  which is what a caller with nowhere to send a key wants rather than
   *  buttons that do nothing when pressed. */
  onVerdict?: (taskId: string, verdict: TaskVerdict) => void;
}): React.ReactElement {
  if (status === 'error') {
    return <Note tone="bad">What it did today could not be read. {error}</Note>;
  }
  if (status !== 'ready' || data === null) return <></>;

  const { wakes, steps, money } = data;

  // Both halves say whether they answered. Neither is folded into a zero: an
  // operator asking why nothing ran needs the reason, and an empty list in
  // front of it reads as an answer.
  if (!steps.read || !money.read) {
    return (
      <Note tone="warn">
        {!money.read
          ? 'What it has spent today could not be read, so nothing ran on its own.'
          : 'What it has been doing could not be read.'}
      </Note>
    );
  }

  if (wakes.length === 0) {
    return <Note>It has not started its day yet.</Note>;
  }

  return (
    <div className="day-room">
      <Band label="What it did" count={wakes.length} />
      <ul className="day-room__wakes">
        {wakes.map((wake) => (
          <li key={`${wake.row}:${wake.at}`} className="day-room__wake">
            <span className="t-meta">{clock(wake.at)}</span>{' '}
            <strong>{ROW_LABEL[wake.row] ?? wake.row}</strong>
            {wake.said ? <span className="t-meta"> {wake.said}</span> : null}
          </li>
        ))}
      </ul>

      {steps.working.length > 0 && (
        <>
          <Band label="In hand" count={steps.working.length} />
          <ul className="day-room__steps">
            {steps.working.map((step) => (
              <li key={step.id}>
                {step.goal} <span className="t-meta">{step.project}</span>
              </li>
            ))}
          </ul>
        </>
      )}

      {steps.closed.length > 0 && (
        <>
          <Band label="Finished today" count={steps.closed.length} />
          <ul className="day-room__steps">
            {steps.closed.map((step, i) => (
              <li key={step.id || `${step.project}:${i}`} className="day-room__step">
                <span>
                  {step.goal}{' '}
                  <span className="t-meta">
                    {[step.project, step.status, `checked by ${step.verifiedBy}`]
                      .filter(Boolean)
                      .join(', ')}
                  </span>
                </span>
                {step.id && onVerdict && (
                  <VerdictKeys
                    taskId={step.id}
                    said={step.said}
                    pending={pendingIds?.has(step.id) ?? false}
                    onPick={onVerdict}
                  />
                )}
              </li>
            ))}
          </ul>
        </>
      )}

      {money.projects.length > 0 && (
        <>
          <Band label="What it cost" count={money.projects.length} />
          <ul className="day-room__money">
            {money.projects.map((row) => (
              <li key={row.id}>
                {row.name}{' '}
                <span className="t-meta">
                  ${row.spent_usd.toFixed(2)} spent
                  {row.left_usd !== null
                    ? `, $${row.left_usd.toFixed(2)} left today`
                    : ''}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}


export function DayRoom(): React.ReactElement {
  const day = useAutonomyStore((s) => s.day);
  const fetchDay = useAutonomyStore((s) => s.fetchDay);
  const pendingActions = useAutonomyStore((s) => s.pendingActions);
  const recordVerdict = useAutonomyStore((s) => s.recordVerdict);

  // The rail fills this whether or not the room is open, so opening it is
  // usually free. A room that lands on an empty store asks once.
  useEffect(() => {
    if (day.status === 'idle') void fetchDay();
  }, [day.status, fetchDay]);

  return (
    <DayRoomView
      data={day.data}
      status={day.status}
      error={day.error}
      pendingIds={pendingActions}
      onVerdict={(taskId, verdict) => void recordVerdict(taskId, verdict)}
    />
  );
}
