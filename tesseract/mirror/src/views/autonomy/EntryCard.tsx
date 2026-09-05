// One thing that runs on its own, opened. Level 3 in every room.
//
// **This file authors nothing about any entry.** What it does, what would be
// lost if it stopped, what it costs and who it serves are the manifest's own
// sentences, rendered as they were written. A job explained here is a job
// whose explanation goes stale the first time the job changes, and the card
// contract exists because that had already happened twice.
//
// The field names are this file's and they are the only words it has:
// `Runs`, `Chain`, `Next`. They name a field rather than describing a job.
// Even the words for `remote_model` and `on_demand` come over the wire, from
// the file that declares those values, for the same reason `not_instrumented`
// does: one vocabulary, translated where the enum lives.

import { useEffect, useState } from 'react';
import { Disclosure } from '../../components/common/Disclosure';
import { Note } from '../../components/common/Note';
import { fetchEntryCard, type EntryCardResponse, type EntryRun } from '../../lib/api';
import { Band, StateStrip } from '../../components/common/StateStrip';
import { Fact } from '../../components/common/Fact';
import { CadenceControl, CeilingControl } from './EntryControls';
import { useAutonomyStore } from '../../stores/autonomy';
import { clock } from '../../lib/time';

export function EntryCardView({
  card,
  onWritten = () => {},
}: {
  card: EntryCardResponse;
  /** Read the card again. What an entry now says after a change is the
   *  backend's answer, never one composed here from what was sent. */
  onWritten?: () => void;
}): React.ReactElement {
  // Which run has its steps showing. One at a time: ten runs of twenty steps
  // open at once is the log this card exists instead of.
  const [openRun, setOpenRun] = useState<string | null>(null);
  return (
    <div className="entry-card">
      <p className="entry-card__summary">{card.summary}</p>

      {/* Only what the entry actually says. A row the operator wrote is not
          in the manifest and never will be, so these arrive empty for it, and
          a heading over nothing is worse than the absence it announces. */}
      {card.why && (
        <>
          <Band label="What would be lost without it" />
          <p className="entry-card__why">{card.why}</p>
        </>
      )}

      <Band label="What it is" />
      <div className="entry-card__facts">
        <Fact label="Runs">{card.runsLabel}</Fact>
        {card.firesOn && <Fact label="Fires on">{card.firesOn}</Fact>}
        {card.kindLabel && <Fact label="Costs">{card.kindLabel}</Fact>}
        {card.ownerLabel && <Fact label="Serves">{card.ownerLabel}</Fact>}
        {card.chainLabels.length > 0 && (
          // One line each, because a chain's line names the models it tries
          // and two of them run together into one unreadable sentence.
          <Fact label="Chain">
            {card.chainLabels.map((said) => (
              <span key={said} className="entry-card__chain">
                {said}
              </span>
            ))}
          </Fact>
        )}
        {card.canSetCeiling ? (
          <CeilingControl
            name={card.name}
            ceiling={card.dailyBudgetUsd}
            onWritten={onWritten}
          />
        ) : (
          card.dailyBudgetUsd !== null && (
            <Fact label="Ceiling a day">{`$${card.dailyBudgetUsd.toFixed(2)}`}</Fact>
          )
        )}
        {/* Beside the ceiling, because the two are the same measurement and a
            figure on its own says nothing about whether it is a lot. */}
        {card.spentToday !== null && (
          <Fact label="Spent today">{`$${card.spentToday.toFixed(2)}`}</Fact>
        )}
        {card.schedule && (
          <>
            {card.canSetCadence ? (
              <CadenceControl name={card.name} cadence={card.schedule.cadence} />
            ) : (
              <Fact label="Cadence">
                {card.schedule.cadence || 'none set'}
                {/* One line, where the control would have been, and it is the
                    backend's sentence rather than one composed here. A field
                    with no `change` beside it and nothing said reads as a
                    surface that forgot to draw the control. */}
                {card.cadenceNotice !== null && (
                  <span className="entry-fact__note t-meta">{card.cadenceNotice}</span>
                )}
              </Fact>
            )}
            {/* Derived from the cadence beside it, so it is read only wherever
                that one is: setting a next time and a cadence separately is
                two answers to when this fires. */}
            <Fact label="Next">
              {card.schedule.enabled
                ? clock(card.schedule.nextFireAt) || 'nothing scheduled'
                : 'turned off'}
            </Fact>
          </>
        )}
        {card.site && <Fact label="Where it runs">{card.site}</Fact>}
        {card.substrate && <Fact label="Started by">{card.substrate}</Fact>}
      </div>

      {/* Said out loud rather than left as an empty field. A card with a blank
          where a cost belongs reads as free. */}
      {card.declared && card.spentToday === null && card.kind !== 'deterministic' && (
        <Note>
          What this one spends is not being counted, because nothing is
          metering on this machine. Every figure on this card that involves
          money is missing for the same reason.
        </Note>
      )}
      {!card.declared && (
        // The app declares what IT ships. This one is the operator's, so the
        // fields above are what their own schedule says and nothing here
        // claims to know what it does or what it costs.
        <Note>
          You wrote this one, so the app makes no claim about what it is for or
          what it costs. What it does is whatever its handler does.
        </Note>
      )}

      <Band label="How it went" count={card.recent.length} />
      {card.recent.length === 0 ? (
        <p className="t-meta">Nothing about it in the run log.</p>
      ) : (
        <StateStrip
          lines={card.recent.map((run, i) => {
            const key = `${run.runId ?? i}`;
            return {
              key,
              state: run.state,
              label: run.label,
              name: run.label,
              said: run.reason || run.outcome,
              when: clock(run.completedAt ?? run.firedAt),
              value: run.trigger,
              // A row is a subgraph, and the only sign of that was a reason
              // that happens to read `<stage>: <what it said>`. What each step
              // is, what it is for and how it went are the run's own record.
              more: <RunSteps run={run} open={openRun === key} onToggle={
                () => setOpenRun((now) => (now === key ? null : key))
              } />,
            };
          })}
        />
      )}
    </div>
  );
}

/** The steps inside one run, and the control that shows them.
 *
 * Under the run rather than in a level of its own: a reader opening one is
 * still reading the history above it, and a level would take that away to
 * show three lines.
 */
function RunSteps({
  run,
  open,
  onToggle,
}: {
  run: EntryRun;
  open: boolean;
  onToggle: () => void;
}): React.ReactElement | null {
  if (run.stages.length === 0) return null;
  const held = run.notDue.length + run.turnedOff.length;
  return (
    <>
      <Disclosure open={open} onToggle={onToggle}>
        {open
          ? 'hide the steps'
          : `${run.stages.length} step${run.stages.length === 1 ? '' : 's'} ran`}
      </Disclosure>
      {open && (
        <>
          <StateStrip
            whole
            lines={run.stages.map((step) => ({
              key: `${run.runId}:${step.stage}`,
              state: step.state,
              label: step.label,
              name: step.stage,
              said: (
                <>
                  {step.reason && <span>{step.reason}</span>}
                  {/* What the step is FOR, which a list of names cannot say.
                      The stage's own sentence, rendered as it was written. */}
                  {step.summary && (
                    <span className="t-meta entry-step__for">{step.summary}</span>
                  )}
                </>
              ),
              when: step.took,
              value: step.changed ? `${step.changed} changed` : '',
            }))}
          />
          {held > 0 && (
            <p className="t-meta entry-step__held">
              {`${held} more did not run this time: ${[...run.notDue, ...run.turnedOff].join(', ')}`}
            </p>
          )}
        </>
      )}
    </>
  );
}

export function EntryCard({ name }: { name: string }): React.ReactElement {
  const [card, setCard] = useState<EntryCardResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bumped after a write, so the card re-reads rather than the writer telling
  // it what it now says.
  const [read, setRead] = useState(0);
  // The other half of the same rule, for the write that answers on the socket:
  // the scheduler says it took a cadence in an envelope, and this is when.
  const touched = useAutonomyStore((s) => s.scheduleTouchedAt);

  useEffect(() => {
    let live = true;
    setError(null);
    fetchEntryCard(name)
      .then((res) => {
        if (live) setCard(res);
      })
      .catch((err: unknown) => {
        if (live) setError(err instanceof Error ? err.message : String(err));
      });
    return () => {
      live = false;
    };
  }, [name, read, touched]);

  // Only on the way to a DIFFERENT entry. Clearing it on every re-read blanked
  // the card for the length of a round trip every time something was changed
  // in it, which reads as the card closing under the operator.
  useEffect(() => setCard(null), [name]);

  if (error) return <Note tone="bad">{error}</Note>;
  if (!card) return <></>;
  return <EntryCardView card={card} onWritten={() => setRead((n) => n + 1)} />;
}
