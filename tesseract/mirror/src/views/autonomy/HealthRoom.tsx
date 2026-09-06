// Health. Is the runtime itself well.
//
// The room the operator reached for and found missing. Three bands, and this
// file decides none of them: which band a department is in, what state it is
// in, what that state is called and what is true of it all arrive in the
// payload. A room that sorted its own rows would be a second definition of
// health, and the first one to go stale.
//
// The judge's steps sit under them as counts, because a judge nobody can
// inspect is a filter, and a filter that silently drops a real fault is the
// failure this phase exists to prevent.
//
// The runtime's own errors are the room's bottom band, which the shell draws.

import { useEffect, useState } from 'react';
import { Button } from '../../components/common/Button';
import { RowActions } from '../../components/common/Row';
import { Disclosure } from '../../components/common/Disclosure';
import { Markdown } from '../../components/common/Markdown';
import { Note } from '../../components/common/Note';
import { useAutonomyStore } from '../../stores/autonomy';
import {
  live,
  NOTHING_PUSHED,
  useLiveContext,
  type LiveContext,
} from '../../stores/liveness';
import type {
  HealthBand,
  HealthDepartment,
  HealthReport,
  HistoryResponse,
  HistorySpent,
} from '../../lib/api';
import { Band, StateStrip, type StateLine } from '../../components/common/StateStrip';
import { HistoryPlots } from './HistoryPlots';
import { freshness } from '../../lib/time';
import { sendCommand } from '../../lib/commands';

// The watchman writes its sweep every quarter of an hour, so this reconciles
// at the slow end of the liveness contract's window rather than polling a file
// into the ground. A department's STATE no longer waits for it:
// `stores/liveness.ts` holds what the runtime pushed, and a change on the
// socket asks this room to read the rest of the row at once.
const POLL_MS = 60_000;

// How long the fortnight answer is worth re-asking for. Mirrors `HELD_FOR` in
// `routes/autonomy_history.py`: the route serves a cached answer for this
// long, so a second request inside the window walks no logs and returns the
// same bytes. Asking anyway is a round trip that cannot change the picture.
const HISTORY_HELD_MS = 120_000;

// What the room says before the feed has answered. The room draws its
// bands empty in that moment and this keeps the report saying the same
// thing they do rather than claiming nothing was ever written.
const NO_REPORT = 'No report has been read yet.';

// The order they are read in: what wants you, what nobody is watching, what is
// working. Naming them here is the only thing this file says about a band, and
// the words are the operator's rather than the enum's.
const BANDS: { key: HealthBand; label: string }[] = [
  { key: 'needs_action', label: 'Needs action' },
  { key: 'blind', label: 'Blind' },
  { key: 'operating', label: 'Operating' },
];

/** One row, and the one thing that may be done to it.
 *
 * The control is `health_leave`, sent as the slash command every tool already
 * has. Not a route of its own: ruling 26 says a control every surface can
 * reach is a tool, and its own text names the shape this takes, that the
 * operator pressing the room's button IS the approval. So the button, a
 * sentence on a channel and the assistant deciding all reach one
 * implementation, and there is no second path to keep in step.
 *
 * A row that asks for nothing carries no button. Offering "leave it alone" on
 * something already quiet is a control with no effect, and a row that looks
 * actionable and is inert is what the panel's own second invariant forbids.
 *
 * A row that IS asking shows its control without being hovered. The
 * reveal-on-hover is right for a long list where acting is occasional and
 * wrong here, for the reason `RowControls` already gives: the operator read
 * every held item, saw a reason and no next step, and said so. A remedy you
 * have to find with the pointer has not been offered.
 */
function line(dept: HealthDepartment, index: number): StateLine {
  const asking = dept.obligation === 'needs_you' || dept.obligation === 'standing_fault';
  const left = dept.acknowledged === true;
  return {
    key: `${dept.band}:${dept.name}:${index}`,
    state: dept.state,
    obligation: dept.obligation,
    label: dept.label,
    name: dept.name,
    said: dept.said,
    // When it was taken, and what it promised. A reading past its own cadence
    // says how late it is: the operator asked to know whether the panel is
    // showing now or showing this morning, and a bare clock cannot answer that.
    when: freshness(dept.at, dept.expectedWithin),
    value: dept.value,
    actions:
      asking || left ? (
        <RowActions
          className={`state-acts${asking ? ' state-acts--waiting' : ''}`}
        >
          <Button
            onClick={() =>
              sendCommand(
                '/health_leave',
                ` subject=${dept.name}${left ? ' action=restore' : ''}`,
              )
            }
            ariaLabel={
              left
                ? `Have ${dept.name} count again`
                : `Leave ${dept.name} alone`
            }
          >
            {left ? 'pick it back up' : 'leave it alone'}
          </Button>
        </RowActions>
      ) : undefined,
  };
}

/** The report the brief sends, in full.
 *
 * It is the one thing in this room that is a document rather than a reading,
 * and it is shut until asked for: the bands above are the room, and a page of
 * markdown open by default would push them off the screen.
 *
 * A report that does not exist and one that could not be opened both say so.
 * Drawing nothing would leave the room implying the machine has never looked
 * at itself, which is the failure the whole panel exists to prevent.
 */
function Report({ report }: { report: HealthReport }): React.ReactElement {
  const [open, setOpen] = useState(false);
  if (report.state !== 'read') {
    return (
      <div className="autonomy-group">
        <Band label="What it wrote down" />
        <Note tone={report.state === 'unknown' ? 'warn' : 'info'}>
          {report.said}
        </Note>
      </div>
    );
  }
  return (
    <div className="autonomy-group">
      <Band label="What it wrote down" />
      <Disclosure
        variant="row"
        open={open}
        onToggle={() => setOpen((was) => !was)}
        ariaControls="health-report"
        testId="health-report-toggle"
      >
        {open ? 'Hide the report' : 'Read the report'}
      </Disclosure>
      {open && (
        <div id="health-report">
          <Markdown>{report.text ?? ''}</Markdown>
          {report.cut && (
            <p className="t-meta">
              This is the start of the report. The whole of it is in the file.
            </p>
          )}
          {report.path && <p className="t-meta">{report.path}</p>}
        </div>
      )}
    </div>
  );
}

/** What each task cost, and which measures the card can make. Two bands over
 *  the payload's lines; the room writes none of the words. */
function Spent({ spent }: { spent: HistorySpent }): React.ReactElement {
  return (
    <>
      <div className="autonomy-group">
        <Band label={spent.title} count={spent.tasks.length} />
        {spent.tasks.length === 0 ? (
          <Note>{spent.empty}</Note>
        ) : (
          <StateStrip lines={spent.tasks} whole />
        )}
      </div>
      <div className="autonomy-group">
        <Band label={spent.measuresTitle} count={spent.measures.length} />
        <StateStrip lines={spent.measures} whole />
      </div>
    </>
  );
}

export function HealthRoomView({
  departments,
  judge,
  report,
  history,
  status,
  error,
  readAt = null,
  liveness = NOTHING_PUSHED,
}: {
  departments: HealthDepartment[];
  judge: { stage: string; kept: number; dropped: number }[];
  report: HealthReport;
  /** Two weeks of the numbers this room is asked about, in the groups the
   *  backend declared, and what each task cost. Null until the history feed
   *  lands, and the plots are simply not there until it does: the bands below
   *  are the room, and a spinner over them would say the room could not be
   *  read. */
  history: HistoryResponse | null;
  status: 'idle' | 'loading' | 'ready' | 'error';
  error: string | null;
  /** When this payload was read. What the runtime pushed wins only while it is
   *  newer than this. */
  readAt?: number | null;
  /** What the runtime has pushed since. Passed in rather than read here, for
   *  the reason the map's own view gives. */
  liveness?: LiveContext;
}): React.ReactElement {
  if (status === 'error') {
    return <Note tone="bad">The health feed could not be read. {error}</Note>;
  }
  if (status !== 'ready') return <></>;

  // One band of plots per group the payload declared, each under its title.
  // The first group leads the room and the rest follow the judge, so what
  // the runtime spent sits with what each task cost rather than over the
  // health bands.
  const groups = history?.groups ?? [];
  const plotsFor = (group: { key: string; title: string }) => (
    <HistoryPlots
      key={group.key}
      label={group.title}
      series={(history?.series ?? []).filter((s) => s.group === group.key)}
    />
  );

  // Every row reads through the one reader, so what a state means when the
  // runtime goes quiet is answered in one place for the whole panel. The band
  // a row is grouped under is left where the payload put it: a band is the
  // rest of a row and arrives with the read this asks for.
  const rows = departments.map((dept) => {
    // What the runtime pushed, over what the room read. The four move
    // together on purpose: a pushed state applied over the row's old
    // obligation would paint a department `failed` in the colour of what it
    // wanted a minute ago and file it under a band it has left.
    const now = live(
      liveness,
      `department:${dept.name}`,
      {
        state: dept.state,
        label: dept.label,
        observedAt: dept.at,
        reason: null,
        obligation: dept.obligation,
        obligationLabel: dept.obligationLabel,
        band: dept.band,
      },
      readAt,
    );
    return {
      ...dept,
      state: now.state,
      label: now.label,
      at: now.observedAt,
      obligation: now.obligation,
      obligationLabel: now.obligationLabel,
      band: now.band,
    };
  });

  return (
    <>
      {/* First, because "is this getting worse" is the question the bands
          underneath cannot answer, and a fortnight is what answers it. */}
      {groups.slice(0, 1).map(plotsFor)}
      {BANDS.map(({ key, label }) => {
        const inBand = rows.filter((d) => d.band === key);
        // An empty band is nothing, not a heading over a space. Blind with
        // nothing in it is the answer everybody wants and it says itself by
        // not being there.
        if (inBand.length === 0) return null;
        return (
          <div key={key} className="autonomy-group">
            <Band label={label} count={inBand.length} />
            <StateStrip lines={inBand.map(line)} />
          </div>
        );
      })}
      {judge.length > 0 && (
        <div className="autonomy-group">
          <Band label="What the judge did with them" />
          <StateStrip
            lines={judge.map((step) => ({
              key: `judge:${step.stage}`,
              state: step.dropped > 0 ? 'degraded' : 'idle',
              name: step.stage,
              said: `${step.kept} kept, ${step.dropped} dropped`,
              value: String(step.kept + step.dropped),
            }))}
          />
        </div>
      )}
      {groups.slice(1).map(plotsFor)}
      {history?.spent && <Spent spent={history.spent} />}
      <Report report={report} />
    </>
  );
}

export function HealthRoom(): React.ReactElement {
  const health = useAutonomyStore((s) => s.health);
  const liveness = useLiveContext(health.data?.labels);
  const fetchHealth = useAutonomyStore((s) => s.fetchHealth);
  const history = useAutonomyStore((s) => s.history);
  const fetchHistory = useAutonomyStore((s) => s.fetchHistory);

  // The first read and the poll share one effect deliberately: `fetchHealth`
  // is a zustand action and its identity is stable for the store's lifetime,
  // so this runs once. Splitting them would add an effect to defend against a
  // dependency nobody has added.
  //
  // The rail renders one room at a time, so leaving Health and coming back
  // unmounts and remounts this. It used to refetch every time, discarding an
  // answer the panel load had already fetched and paying a round trip to
  // redraw the same rows. The age is read off the store rather than
  // subscribed to, so this still runs once: what it needs is the age at the
  // moment of opening, not every later change to it.
  useEffect(() => {
    const { lastFetched } = useAutonomyStore.getState().health;
    const age = lastFetched === null ? Infinity : Date.now() - lastFetched;
    let poll: number | undefined;
    let firstRead: number | undefined;
    const startPolling = (): void => {
      poll = window.setInterval(() => void fetchHealth(), POLL_MS);
    };
    if (age >= POLL_MS) {
      void fetchHealth();
      startPolling();
    } else {
      // Already fresh. Wait out the remainder of its life rather than a whole
      // interval, so re-opening a room cannot make it staler than leaving it
      // open would have.
      firstRead = window.setTimeout(() => {
        void fetchHealth();
        startPolling();
      }, POLL_MS - age);
    }
    return () => {
      if (firstRead !== undefined) window.clearTimeout(firstRead);
      if (poll !== undefined) window.clearInterval(poll);
    };
  }, [fetchHealth]);

  // Asked for when the room opens rather than on every panel load: no rail
  // mark reads it, and it opens a few megabytes of log to answer. Read once,
  // because a fortnight does not move while somebody is looking at it, and
  // not again while the answer is younger than the cache the route serves it
  // from: re-opening inside that window can only be told the same thing.
  useEffect(() => {
    const { lastFetched } = useAutonomyStore.getState().history;
    if (lastFetched === null || Date.now() - lastFetched >= HISTORY_HELD_MS) {
      void fetchHistory();
    }
  }, [fetchHistory]);

  return (
    <HealthRoomView
      departments={health.data?.departments ?? []}
      readAt={health.lastFetched}
      liveness={liveness}
      judge={health.data?.judge ?? []}
      report={health.data?.report ?? { state: 'none', said: NO_REPORT }}
      history={history.data ?? null}
      status={health.status}
      error={health.error}
    />
  );
}
