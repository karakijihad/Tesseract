import { useEffect } from 'react';
import { useConscienceStore, type DriftReport, type SignalResult } from '../stores/conscience';
import { formatRelative } from '../lib/time';

export function ConscienceView() {
  const report = useConscienceStore((s) => s.report);
  const history = useConscienceStore((s) => s.history);
  const loading = useConscienceStore((s) => s.loading);
  const error = useConscienceStore((s) => s.error);
  const fetchDrift = useConscienceStore((s) => s.fetchDrift);

  useEffect(() => {
    fetchDrift();
  }, [fetchDrift]);

  return (
    <div className="conscience-view">
      <header className="conscience-view-head">
        <div className="conscience-view-title-row">
          <h1 className="t-head conscience-view-title">Conscience</h1>
          <span className="t-meta conscience-view-meta">
            {report
              ? `Last scrape ${formatRelative(report.timestamp)} · ${report.window_hours}h window · ${report.signals.length} signals`
              : 'Awaiting first scrape — enable conscience_heartbeat on the Schedule tab.'}
          </span>
        </div>
        <button
          type="button"
          className="conscience-view-refresh"
          onClick={() => fetchDrift()}
          disabled={loading}
        >
          {loading ? '…' : 'refresh'}
        </button>
      </header>

      {error && (
        <div className="conscience-view-error t-caption">Error loading drift: {error}</div>
      )}

      {!report ? (
        <EmptyState />
      ) : (
        <div className="conscience-view-body">
          <Summary report={report} />
          <div className="conscience-view-grid">
            {report.signals.map((sig) => (
              <SignalCard key={sig.name} sig={sig} />
            ))}
          </div>
          {history.length > 1 && <History history={history} />}
        </div>
      )}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="conscience-view-empty t-caption">
      No drift reports yet. The <code>conscience_heartbeat</code> job runs daily and ships
      disabled by default — toggle it on in the Schedule tab, then come back after it fires
      (07:00 UTC) to see signal state.
    </div>
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

function History({ history }: { history: DriftReport[] }) {
  return (
    <section className="conscience-history">
      <div className="conscience-history-heading t-meta">Last {history.length} reports</div>
      <div className="conscience-history-bars">
        {history.map((h, i) => {
          const total = h.summary.ok + h.summary.warn + h.summary.bad || 1;
          return (
            <div
              key={`${h.timestamp}-${i}`}
              className="conscience-history-bar"
              title={`${h.timestamp} — ok=${h.summary.ok} warn=${h.summary.warn} bad=${h.summary.bad}`}
            >
              {h.summary.bad > 0 && (
                <span
                  className="conscience-history-seg conscience-history-seg--bad"
                  style={{ flex: h.summary.bad / total }}
                />
              )}
              {h.summary.warn > 0 && (
                <span
                  className="conscience-history-seg conscience-history-seg--warn"
                  style={{ flex: h.summary.warn / total }}
                />
              )}
              {h.summary.ok > 0 && (
                <span
                  className="conscience-history-seg conscience-history-seg--ok"
                  style={{ flex: h.summary.ok / total }}
                />
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
