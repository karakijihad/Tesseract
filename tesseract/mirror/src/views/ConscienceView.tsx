import { useEffect } from 'react';
import { useConscienceStore, type DriftReport, type SignalResult } from '../stores/conscience';
import { useScheduleStore } from '../stores/schedule';
import { useRefreshOnVisible } from '../hooks/usePanelVisible';
import { formatRelative } from '../lib/time';
import { Block } from '../components/common/Block';
import { Button } from '../components/common/Button';
import { Input } from '../components/common/Input';
import { Note } from '../components/common/Note';
import { RailView, type RailGroup } from '../components/common/RailView';
import { DriftHistoryChart } from './conscience/DriftHistoryChart';
import { ToolUsageChart } from './conscience/ToolUsageChart';
import { nextFireTime } from './schedule/cadence';

// The job this tab is about. Its cadence and its enabled state are NOT
// written here — they live in `schedule.yaml` and arrive through the schedule
// store, because every string this view showed about them had drifted: it told
// the operator to enable a job that was already on, at a time it does not run.
const HEARTBEAT_JOB = 'conscience_heartbeat';

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
  return {
    registered: !!job,
    enabled: job?.runtime?.enabled ?? job?.enabled ?? false,
    cadence,
    nextFire: cadence ? nextFireTime(cadence) : null,
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

  // The panel stays mounted when it closes, so a mount effect fetched once per
  // app launch and the tab showed whatever it read at boot. This covers the
  // first open too — opening a panel IS a hidden→visible transition.
  useRefreshOnVisible('conscience', fetchDrift);

  const groups: RailGroup[] = [
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
                  {report.summary.warn === 0 && report.summary.bad === 0 && (
                    <Note>
                      Every signal is ok — nothing is drifting. That is the
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
      ],
    },
  ];

  return (
    <RailView
      groups={groups}
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
  if (!facts.registered) return `Awaiting first scrape — ${HEARTBEAT_JOB} is not registered.`;
  if (!facts.enabled) return `No scrape yet — ${HEARTBEAT_JOB} is switched off.`;
  if (facts.nextFire) return `No scrape yet — next run ${formatClock(facts.nextFire)}.`;
  return `No scrape yet — ${HEARTBEAT_JOB} runs on ${facts.cadence}.`;
}

function EmptyState({ facts }: { facts: JobFacts }) {
  if (!facts.registered) {
    return (
      <Note tone="warn">
        No drift reports, and no <code>{HEARTBEAT_JOB}</code> job to write one. Nothing is
        watching for drift until it is added on the Schedule tab.
      </Note>
    );
  }
  if (!facts.enabled) {
    return (
      <Note tone="warn">
        No drift reports yet — <code>{HEARTBEAT_JOB}</code> is switched off. Turn it on in
        the Schedule tab, or run it once from there to fill this in now.
      </Note>
    );
  }
  return (
    <Note>
      No drift reports yet. <code>{HEARTBEAT_JOB}</code> is on and runs{' '}
      <code>{facts.cadence}</code>
      {facts.nextFire ? ` — next at ${formatClock(facts.nextFire)}` : ''}. This tab fills
      itself when it does; there is nothing to enable. To see it now, run the job from the
      Schedule tab. An empty tab means no report has been written yet — it is not the same
      as a report with nothing wrong in it, which is what a healthy install looks like.
    </Note>
  );
}

function Summary({ report }: { report: DriftReport }) {
  return (
    <section className="conscience-summary" aria-label="Drift summary">
      <SummaryPill kind="ok" count={report.summary.ok} />
      <SummaryPill kind="warn" count={report.summary.warn} />
      <SummaryPill kind="bad" count={report.summary.bad} />
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
      meta="a readout — what loads every turn is set in code until IS-11"
    >
      <ToolUsageChart windows={windows} total={total} roster={roster} />
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
        No reports stored yet — the trend appears once the job has fired.
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
          One report in this window — widen it, or wait for the job to fire
          again, for a trend to draw.
        </Note>
      ) : (
        <DriftHistoryChart history={history} />
      )}
    </Block>
  );
}
