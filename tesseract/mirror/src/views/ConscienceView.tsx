import { useEffect, useMemo, useState } from 'react';
import { driftCounts, useConscienceStore, type DriftReport, type SignalResult } from '../stores/conscience';
import { useScheduleStore } from '../stores/schedule';
import { useRefreshOnVisible } from '../hooks/usePanelVisible';
import { formatRelative } from '../lib/time';
import { readCadence } from '../lib/api';
import { Block } from '../components/common/Block';
import { Button } from '../components/common/Button';
import { Input } from '../components/common/Input';
import { Note } from '../components/common/Note';
import { RailView, type RailGroup } from '../components/common/RailView';
import { CacheReport } from './conscience/CacheReport';
import { DayPanel } from './conscience/DayPanel';
import { DriftHistoryChart } from './conscience/DriftHistoryChart';
import { PayloadBreakdown } from './conscience/PayloadBreakdown';
import { PlaybookUsageChart } from './conscience/PlaybookUsageChart';
import { ToolHeatmap } from './conscience/ToolHeatmap';
import { ToolUsageChart } from './conscience/ToolUsageChart';
import { WorkingSetPanel } from './conscience/WorkingSetPanel';

// The job this tab is about. Its cadence and its enabled state are NOT
// written here — they live in `schedule.yaml` and arrive through the schedule
// store, because every string this view showed about them had drifted: it told
// the operator to enable a job that was already on, at a time it does not run.
const HEARTBEAT_JOB = 'conscience_heartbeat';

/** Payload rows whose surface is a section of THIS panel, keyed by the name
 *  the assembly gives the block. The tool schemas are as big as the working
 *  set is, so "go and look" is one rail row away rather than another window.
 */
const PAYLOAD_JUMPS: Record<string, string> = { schemas: 'working-set' };

interface JobFacts {
  registered: boolean;
  enabled: boolean;
  cadence: string;
  nextFire: Date | null;
}

function useHeartbeatFacts(): JobFacts {
  const jobs = useScheduleStore((s) => s.jobs);
  const fetchJobs = useScheduleStore((s) => s.fetchJobs);
  useEffect(() => {
    if (jobs.length === 0) void fetchJobs();
  }, [jobs.length, fetchJobs]);
  const job = jobs.find((j) => j.name === HEARTBEAT_JOB);
  const cadence = job?.runtime?.cadence ?? job?.cadence ?? '';
  // Asked, not worked out. When this computed its own next fire it was a
  // second reading of the scheduler's grammar, and a second reading is the
  // one that ends up disagreeing.
  const [nextFire, setNextFire] = useState<Date | null>(null);
  useEffect(() => {
    if (!cadence) {
      setNextFire(null);
      return;
    }
    let live = true;
    readCadence(cadence)
      .then((r) => {
        if (live) setNextFire(r.nextFireAt ? new Date(r.nextFireAt) : null);
      })
      .catch(() => {
        if (live) setNextFire(null);
      });
    return () => {
      live = false;
    };
  }, [cadence]);
  return {
    registered: !!job,
    enabled: job?.runtime?.enabled ?? job?.enabled ?? false,
    cadence,
    nextFire,
  };
}

function formatClock(when: Date): string {
  return when.toLocaleTimeString([], {
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function ConscienceView() {
  const report = useConscienceStore((s) => s.report);
  const loading = useConscienceStore((s) => s.loading);
  const error = useConscienceStore((s) => s.error);
  const fetchDrift = useConscienceStore((s) => s.fetchDrift);
  const facts = useHeartbeatFacts();
  // The view owns which section is open, because one section links to
  // another: the payload's tool row is answered by the working set, and
  // opening the panel the reader is already inside would do nothing.
  const [section, setSection] = useState('signals');

  // The panel stays mounted when it closes, so a mount effect fetched once per
  // app launch and the tab showed whatever it read at boot. This covers the
  // first open too — opening a panel IS a hidden→visible transition.
  useRefreshOnVisible('conscience', fetchDrift);

  const groups: RailGroup[] = [
    {
      label: 'What it did',
      sections: [
        {
          key: 'day',
          label: 'By day',
          render: () => <DayPanel />,
        },
      ],
    },
    {
      label: 'Drift',
      sections: [
        {
          key: 'signals',
          label: 'Signals',
          render: () => (
            <>
              {error && <Note tone="bad">Error loading drift: {error}</Note>}
              {!report ? (
                <EmptyState facts={facts} />
              ) : (
                <>
                  <Summary report={report} />
                  {driftCounts(report).warn === 0 && driftCounts(report).bad === 0 && (
                    <Note>
                      Every signal is ok, and nothing has moved. That is the
                      healthy result, not an empty tab.
                    </Note>
                  )}
                  <div className="conscience-view-grid">
                    {report.signals.map((sig) => (
                      <SignalCard key={sig.name} sig={sig} />
                    ))}
                  </div>
                </>
              )}
            </>
          ),
        },
        {
          key: 'history',
          label: 'History',
          render: () => <History />,
        },
      ],
    },
    {
      label: 'Usage',
      sections: [
        {
          key: 'tools',
          label: 'Tools',
          render: () => <ToolUsage />,
        },
        {
          key: 'playbooks',
          label: 'Playbooks',
          render: () => <PlaybookUsage />,
        },
        {
          key: 'working-set',
          label: 'Working set',
          render: () => <WorkingSet />,
        },
        {
          key: 'heatmap',
          label: 'Over time',
          render: () => <Heatmap />,
        },
        {
          key: 'cache',
          label: 'Cache',
          render: () => <Cache />,
        },
        {
          key: 'payload',
          label: 'Payload',
          render: () => (
            <Payload
              onJump={(name) => {
                const to = PAYLOAD_JUMPS[name];
                if (!to) return false;
                setSection(to);
                return true;
              }}
            />
          ),
        },
      ],
    },
  ];

  return (
    <RailView
      view="conscience"
      groups={groups}
      active={section}
      onSectionChange={setSection}
      label="Conscience sections"
      meta={
        report
          ? `Last scrape ${formatRelative(report.timestamp)} · ${report.window_hours}h window · ${report.signals.length} signals`
          : describeWait(facts)
      }
      actions={
        <Button
          onClick={() => fetchDrift()}
          disabled={loading}
          ariaLabel="refresh drift report"
        >
          {loading ? '…' : 'refresh'}
        </Button>
      }
    />
  );
}

function describeWait(facts: JobFacts): string {
  if (!facts.registered) return `Awaiting the first scrape. ${HEARTBEAT_JOB} is not registered.`;
  if (!facts.enabled) return `No scrape yet. ${HEARTBEAT_JOB} is switched off.`;
  if (facts.nextFire) return `No scrape yet. The next run is ${formatClock(facts.nextFire)}.`;
  return `No scrape yet. ${HEARTBEAT_JOB} runs on ${facts.cadence}.`;
}

function EmptyState({ facts }: { facts: JobFacts }) {
  if (!facts.registered) {
    return (
      <Note tone="warn">
        No drift reports, and no <code>{HEARTBEAT_JOB}</code> job to write one. Nothing is
        watching for drift until it is added in the Autonomy panel, under Managed system.
      </Note>
    );
  }
  if (!facts.enabled) {
    return (
      <Note tone="warn">
        No reports yet. <code>{HEARTBEAT_JOB}</code> is switched off; turn it on in the
        Autonomy panel, under Managed system, or run it once from there to fill this in now.
      </Note>
    );
  }
  return (
    <Note>
      No drift reports yet. <code>{HEARTBEAT_JOB}</code> is on and runs{' '}
      <code>{facts.cadence}</code>
      {facts.nextFire ? `, next at ${formatClock(facts.nextFire)}` : ''}. This tab fills
      itself when it does; there is nothing to enable. To see it now, run the job from the
      Autonomy panel, under Managed system. An empty tab means no report has been written
      yet, which is not the same
      as a report with nothing wrong in it, which is what a healthy install looks like.
    </Note>
  );
}

function Summary({ report }: { report: DriftReport }) {
  return (
    <section className="conscience-summary" aria-label="Drift summary">
      <SummaryPill kind="ok" count={driftCounts(report).ok} />
      <SummaryPill kind="warn" count={driftCounts(report).warn} />
      <SummaryPill kind="bad" count={driftCounts(report).bad} />
    </section>
  );
}

function SummaryPill({ kind, count }: { kind: 'ok' | 'warn' | 'bad'; count: number }) {
  return (
    <div className={`conscience-summary-pill conscience-summary-pill--${kind}`}>
      <span className="conscience-summary-count">{count}</span>
      <span className="t-meta">{kind}</span>
    </div>
  );
}

function SignalCard({ sig }: { sig: SignalResult }) {
  return (
    <section className={`conscience-card conscience-card--${sig.status}`}>
      <div className="conscience-card-heading t-meta">{sig.name}</div>
      <div className="conscience-card-value">
        <span className="conscience-card-number">{formatValue(sig)}</span>
        <span className={`conscience-card-status conscience-card-status--${sig.status}`}>
          {sig.status}
        </span>
      </div>
      <div className="t-caption conscience-card-thresholds">
        warn ≥ {sig.warn} · bad ≥ {sig.bad}
      </div>
      {sig.detail && <div className="t-caption conscience-card-detail">{sig.detail}</div>}
    </section>
  );
}

function formatValue(sig: SignalResult): string {
  if (sig.name === 'scheduler_failure_rate') {
    return `${(sig.value * 100).toFixed(1)}%`;
  }
  if (sig.name === 'scheduler_idle_hours') {
    return `${sig.value.toFixed(1)}h`;
  }
  return String(sig.value);
}

/** The trend, over a window the operator chooses.
 *
 * Bounded by `available` — the dates that actually have a report on disk — so
 * the picker cannot be set to a range that returns nothing. A calendar that
 * offers every day since 1970 against five files on disk is a control that is
 * wrong more often than it is right, and an empty chart from it reads as a
 * broken panel rather than as an empty week.
 */
/** Which tools get used, over a window.
 *
 * Its own group rather than a third section under Drift: drift is about
 * whether the assistant is still itself, and this is about which of its
 * capabilities it reaches for. Filing them together would make one of the two
 * headings a lie.
 */
function ToolUsage() {
  const windows = useConscienceStore((s) => s.usage);
  const total = useConscienceStore((s) => s.usageTotal);
  const roster = useConscienceStore((s) => s.usageRoster);
  const loading = useConscienceStore((s) => s.usageLoading);
  const error = useConscienceStore((s) => s.usageError);
  const fetchToolUsage = useConscienceStore((s) => s.fetchToolUsage);
  const workingSet = useConscienceStore((s) => s.workingSet);
  const fetchWorkingSet = useConscienceStore((s) => s.fetchWorkingSet);

  // The roster too, because the reading this panel is for is the
  // intersection: which of the tools nobody calls are being described every
  // turn anyway. Two reads rather than one endpoint returning both, because
  // they are two questions and the next section asks the other one.
  useRefreshOnVisible('conscience', fetchWorkingSet);

  const carried = useMemo(
    () =>
      workingSet
        ? new Set(workingSet.tools.filter((t) => t.core).map((t) => t.tool))
        : null,
    [workingSet],
  );

  // The panel, not a section id — `PanelKind` names cockpit panels and there
  // is one Conscience panel. This component only mounts when its section is
  // selected, and the hook fires on that mount, so switching to Tools is what
  // fetches rather than opening the panel at all.
  useRefreshOnVisible('conscience', fetchToolUsage);

  if (error) return <Note tone="bad">Error loading tool usage: {error}</Note>;
  if (!windows.length) {
    return loading ? (
      <Note>Reading the usage ledger…</Note>
    ) : (
      <Note>
        No tool calls recorded yet. The ledger writes one row per call, so this
        fills in as the assistant works.
      </Note>
    );
  }

  return (
    <Block
      title={null}
      meta="What it reached for. Which tools it carries is the next section."
    >
      <ToolUsageChart
        windows={windows}
        total={total}
        roster={roster}
        carried={carried}
      />
    </Block>
  );
}

/** What the prompt cache did, beside what the payload costs.
 *
 * Its own section rather than a figure on the payload panel: that one answers
 * what a turn carries before you type, which is a composition and holds still.
 * This one answers what the provider had already read, which changes with
 * every turn and is the number the payload is being shaped to move.
 */
function Cache() {
  const reading = useConscienceStore((s) => s.cache);
  const loading = useConscienceStore((s) => s.cacheLoading);
  const error = useConscienceStore((s) => s.cacheError);
  const fetchCache = useConscienceStore((s) => s.fetchCache);

  useRefreshOnVisible('conscience', fetchCache);

  if (error) return <Note tone="bad">Error reading the cost ledger: {error}</Note>;
  if (!reading) {
    return loading ? <Note>Reading the cost ledger…</Note> : <Note>Nothing to show yet.</Note>;
  }
  if (!reading.ledger) {
    return (
      <Note>
        No cost ledger has been written yet. It fills in as the assistant works,
        one row per model call.
      </Note>
    );
  }
  if (!reading.summary || reading.summary.calls === 0) {
    return (
      <Note>
        No model calls in the last {reading.days}{' '}
        {reading.days === 1 ? 'day' : 'days'}.
      </Note>
    );
  }

  return (
    <Block
      title={null}
      meta="What the provider had already read. What a turn carries is the next section."
    >
      <CacheReport reading={reading} />
    </Block>
  );
}

/** The other thing a turn carries, and the one that can say whether it worked.
 *
 * Its own section beside Tools rather than a second series on that chart: the
 * tool ledger records that a call happened and nothing about how it went, and
 * a playbook load carries the revision and the session, so the turn records
 * can say how the work that followed ended. Two different questions with two
 * different answers is two charts.
 */
function PlaybookUsage() {
  const usage = useConscienceStore((s) => s.playbookUsage);
  const loading = useConscienceStore((s) => s.playbookUsageLoading);
  const error = useConscienceStore((s) => s.playbookUsageError);
  const fetchPlaybookUsage = useConscienceStore((s) => s.fetchPlaybookUsage);

  useRefreshOnVisible('conscience', fetchPlaybookUsage);

  if (error) return <Note tone="bad">Error reading playbook usage: {error}</Note>;
  if (!usage) {
    return loading ? <Note>Reading the usage log…</Note> : <Note>Nothing to show yet.</Note>;
  }
  if (usage.total === 0) {
    return (
      <Note>
        There are no playbooks yet. One is written when a task the assistant
        finished is worth doing the same way again.
      </Note>
    );
  }

  return (
    <Block
      title={null}
      meta="What it reached for, and how the work that followed went."
    >
      <PlaybookUsageChart
        rows={usage.playbooks}
        days={usage.days}
        carriedCount={usage.carried_count}
        path={usage.path}
        turnScopeNote={usage.turn_scope_note}
      />
    </Block>
  );
}

/** The dial itself: which tools are described on every turn.
 *
 * Its own section rather than a control bolted onto the bars. The bars answer
 * "what did it reach for", which is history and cannot be edited; this answers
 * "what does it carry", which is a setting. Reading the first is how you
 * decide the second, so they sit next to each other rather than in one figure
 * that is half chart and half form.
 */
function WorkingSet() {
  const workingSet = useConscienceStore((s) => s.workingSet);
  const loading = useConscienceStore((s) => s.workingSetLoading);
  const error = useConscienceStore((s) => s.workingSetError);
  const saving = useConscienceStore((s) => s.workingSetSaving);
  const fetchWorkingSet = useConscienceStore((s) => s.fetchWorkingSet);
  const setToolCore = useConscienceStore((s) => s.setToolCore);

  useRefreshOnVisible('conscience', fetchWorkingSet);

  if (error && !workingSet) return <Note tone="bad">Error reading the working set: {error}</Note>;
  if (!workingSet) {
    return loading ? <Note>Reading the tool list…</Note> : <Note>Nothing to show yet.</Note>;
  }

  return (
    <Block title={null}>
      {error && <Note tone="bad">{error}</Note>}
      <WorkingSetPanel
        rows={workingSet.tools}
        groups={workingSet.groups}
        coreCount={workingSet.core_count}
        total={workingSet.total}
        path={workingSet.path}
        customPath={workingSet.custom_path ?? workingSet.path}
        saving={saving}
        onToggle={(tool, core) => void setToolCore(tool, core)}
      />
    </Block>
  );
}

/** The same ledger, shaped as days. */
function Heatmap() {
  const heatmap = useConscienceStore((s) => s.heatmap);
  const days = useConscienceStore((s) => s.heatmapDays);
  const loading = useConscienceStore((s) => s.heatmapLoading);
  const error = useConscienceStore((s) => s.heatmapError);
  const fetchHeatmap = useConscienceStore((s) => s.fetchHeatmap);

  useRefreshOnVisible('conscience', fetchHeatmap);

  if (error) return <Note tone="bad">Error reading the usage ledger: {error}</Note>;
  if (!heatmap) {
    return loading ? <Note>Reading the usage ledger…</Note> : <Note>Nothing to show yet.</Note>;
  }

  return (
    <Block title={null} meta="One square per tool per day. Darker is more calls.">
      <ToolHeatmap
        data={heatmap}
        days={days}
        loading={loading}
        onWindow={(next) => void fetchHeatmap(next)}
      />
    </Block>
  );
}


function Payload({ onJump }: { onJump: (name: string) => boolean }) {
  const payload = useConscienceStore((s) => s.payload);
  const loading = useConscienceStore((s) => s.payloadLoading);
  const error = useConscienceStore((s) => s.payloadError);
  const fetchPayload = useConscienceStore((s) => s.fetchPayload);

  // Same reasoning as `ToolUsage`: this component only mounts when its
  // section is selected, so switching to Payload is what re-measures. It has
  // to be a fresh reading — the prompt changes with the workspace, the memory
  // store and the registry, and a cached one would say what a turn cost the
  // last time anybody looked.
  useRefreshOnVisible('conscience', fetchPayload);

  if (error) return <Note tone="bad">Error composing the payload: {error}</Note>;
  if (!payload?.readings.length) {
    return loading ? (
      <Note>Assembling a turn…</Note>
    ) : (
      <Note>Nothing measured yet.</Note>
    );
  }

  return (
    <Block
      title={null}
      meta="One real turn, measured by assembling it."
    >
      <PayloadBreakdown readings={payload.readings} onJump={onJump} />
    </Block>
  );
}

function History() {
  const history = useConscienceStore((s) => s.history);
  const available = useConscienceStore((s) => s.available);
  const range = useConscienceStore((s) => s.range);
  const loading = useConscienceStore((s) => s.loading);
  const fetchDrift = useConscienceStore((s) => s.fetchDrift);

  const earliest = available[0];
  const latest = available[available.length - 1];

  // The presets are days back from the newest report, not from today: on an
  // install whose heartbeat has been off for a week, "last 7 days" measured
  // from today is guaranteed empty.
  //
  // Formatted from LOCAL parts, never `toISOString()`. The dates here are
  // calendar days — a file is named `drift-2026-08-08.jsonl` — and
  // `toISOString` converts to UTC first, so local midnight anywhere east of
  // Greenwich reports the previous day. "7 days" asked for the 7th instead of
  // the 8th before this said so.
  const preset = (days: number) => {
    if (!latest) return;
    const start = new Date(`${latest}T00:00:00`);
    start.setDate(start.getDate() - (days - 1));
    const iso = (d: Date) =>
      `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(
        d.getDate(),
      ).padStart(2, '0')}`;
    void fetchDrift({ from: iso(start), to: latest });
  };

  if (available.length === 0) {
    return (
      <Note>
        No reports stored yet. The trend appears once the job has run.
      </Note>
    );
  }

  return (
    <Block
      title={null}
      meta={`${history.length} of ${available.length} stored reports`}
    >
      <div className="conscience-range">
        <label className="conscience-range__field">
          <span className="t-meta">From</span>
          <Input
            type="date"
            value={range.from ?? ''}
            min={earliest}
            max={range.to ?? latest}
            disabled={loading}
            ariaLabel="Drift range start"
            onChange={(next) => void fetchDrift({ from: next, to: range.to })}
          />
        </label>
        <label className="conscience-range__field">
          <span className="t-meta">To</span>
          <Input
            type="date"
            value={range.to ?? ''}
            min={range.from ?? earliest}
            max={latest}
            disabled={loading}
            ariaLabel="Drift range end"
            onChange={(next) => void fetchDrift({ from: range.from, to: next })}
          />
        </label>
        <Button onClick={() => preset(7)} disabled={loading}>
          7 days
        </Button>
        <Button onClick={() => preset(30)} disabled={loading}>
          30 days
        </Button>
        <Button
          onClick={() => void fetchDrift({ from: earliest ?? null, to: latest ?? null })}
          disabled={loading}
        >
          All {available.length}
        </Button>
        <span className="t-meta conscience-range__stored">
          stored locally: {earliest} → {latest}
        </span>
      </div>

      {history.length === 0 ? (
        <Note>
          No report was written between those dates. The days with one are{' '}
          {earliest} to {latest}.
        </Note>
      ) : history.length === 1 ? (
        <Note>
          One report in this window. Widen it, or wait for the job to run
          again, for a trend to draw.
        </Note>
      ) : (
        <DriftHistoryChart history={history} />
      )}
    </Block>
  );
}
