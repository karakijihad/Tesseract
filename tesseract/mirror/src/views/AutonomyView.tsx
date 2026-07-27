// AU-7 — Autonomy Dashboard default view.
//
// Single screen that lets the operator answer the six GOVERNANCE §15
// questions without tab clicks:
//
//   Q1 (what is TARS doing right now?) → AgendaPane + WorkersPane
//   Q2 (why?)                          → AgendaPane + DetailModal rationale
//   Q3 (what can it do without me?)    → AgendaPane (risk_class chip)
//   Q4 (what needs my approval?)       → ApprovalsPane
//   Q5 (what survived last restart?)   → RecoveryPane
//   Q6 (what failed and when?)         → DecisionLogPane + BlockedPane
//
// S1 shipped the read-only surface; S2 adds Approve / Cancel / Snooze /
// Boost / Unpause / Shutdown actions + the click-row detail modal with
// score breakdown.
//
// 2026-05-18: scheduled-jobs pane removed from the autonomy sidebar —
// scheduled work is the Schedule tab's responsibility; surfacing it
// here doubled the operator's cognitive load without adding signal.

import { useEffect, useState } from 'react';
import { useAutonomyStore } from '../stores/autonomy';
import { AgendaDetailModal } from './autonomy/AgendaDetailModal';
import { AgendaPane } from './autonomy/AgendaPane';
import { ApprovalsPane } from './autonomy/ApprovalsPane';
import { BlockedPane } from './autonomy/BlockedPane';
import { CodeDriftChip } from './autonomy/CodeDriftChip';
import { DecisionLogPane } from './autonomy/DecisionLogPane';
import { JournalPane } from './autonomy/JournalPane';
import { NotificationsPane } from './autonomy/NotificationsPane';
import { PrunedPane } from './autonomy/PrunedPane';
import { RecoveryPane, type RecoverySummaryPayload } from './autonomy/RecoveryPane';
import { RuntimeSection } from './settings/Runtime';
import { WorkerDetailModal } from './autonomy/WorkerDetailModal';
import { WorkersPane } from './autonomy/WorkersPane';

const SHUTDOWN_KEY = 'runtime:shutdown';

// `workers/active/` retains terminal records (done/failed/cancelled/…)
// until the archive janitor sweeps them. The header count is meant as a
// liveness signal — count live statuses only so it doesn't read "83
// workers" against a fleet of corpses.
const LIVE_WORKER_STATUSES = new Set([
  'queued',
  'spawning',
  'running',
  'awaiting_io',
]);

export function AutonomyView(): React.ReactElement {
  const agenda = useAutonomyStore((s) => s.agenda);
  const workers = useAutonomyStore((s) => s.workers);
  const governor = useAutonomyStore((s) => s.governor);
  const recovery = useAutonomyStore((s) => s.recovery);
  const journal = useAutonomyStore((s) => s.journal);
  const fetchAll = useAutonomyStore((s) => s.fetchAll);
  const selectedAgendaId = useAutonomyStore((s) => s.selectedAgendaId);
  const closeDetail = useAutonomyStore((s) => s.closeDetail);
  const runtimeShutdown = useAutonomyStore((s) => s.runtimeShutdown);
  const pending = useAutonomyStore((s) => s.pendingActions);
  const shutdownBusy = pending.has(SHUTDOWN_KEY);

  // Confirm-then-fire: first click arms the button, second click within
  // 6s actually calls /api/runtime/shutdown. Inline rather than a
  // separate modal — keeps the chrome lean and the action is rare.
  const [shutdownArmed, setShutdownArmed] = useState(false);
  useEffect(() => {
    if (!shutdownArmed) return;
    const t = setTimeout(() => setShutdownArmed(false), 6000);
    return () => clearTimeout(t);
  }, [shutdownArmed]);

  // The store self-hydrates on WS connect (websocket.ts onopen). The
  // manual refresh below covers a hard refresh that lands on this view
  // before the WS has opened. Idempotent — Promise.allSettled internally.
  useEffect(() => {
    if (agenda.status === 'idle') {
      void fetchAll();
    }
  }, [agenda.status, fetchAll]);

  const recoveryPayload: RecoverySummaryPayload | null =
    recovery.data?.recovery ? { ...recovery.data.recovery } : null;
  const recoveryState: 'recovering' | 'ready' = recovery.data?.state ?? 'ready';

  const selectedItem = selectedAgendaId
    ? agenda.data.find((i) => i.id === selectedAgendaId) ?? null
    : null;

  // Detail modal is opened by clicking a row in any pane. If the store's
  // current agenda fetch doesn't include the selected id (it got
  // archived between click + render), close the modal so the operator
  // doesn't see a stuck overlay against stale data.
  useEffect(() => {
    if (selectedAgendaId && !selectedItem) {
      closeDetail();
    }
  }, [selectedAgendaId, selectedItem, closeDetail]);

  const onShutdownClick = () => {
    if (shutdownBusy) return;
    if (!shutdownArmed) {
      setShutdownArmed(true);
      return;
    }
    setShutdownArmed(false);
    void runtimeShutdown();
  };

  let shutdownLabel = 'shutdown';
  if (shutdownBusy) shutdownLabel = 'shutting down…';
  else if (shutdownArmed) shutdownLabel = 'confirm shutdown';

  const liveWorkerCount = workers.data.filter((w) =>
    LIVE_WORKER_STATUSES.has(w.status),
  ).length;

  return (
    <div className="autonomy-view" data-testid="autonomy-view">
      <header className="autonomy-view__head">
        <span className="autonomy-view__title">Autonomy</span>
        <span className="t-meta">
          {governor.data?.running ? 'governor running' : 'governor offline'}
          {' · '}
          {liveWorkerCount} worker{liveWorkerCount === 1 ? '' : 's'}
          {' · '}
          {agenda.data.length} agenda
        </span>
        <CodeDriftChip />
        <button
          type="button"
          className="autonomy-view__refresh"
          onClick={() => void fetchAll()}
          aria-label="refresh autonomy state"
        >
          refresh
        </button>
        <button
          type="button"
          className={`autonomy-view__shutdown${shutdownArmed ? ' is-armed' : ''}`}
          onClick={onShutdownClick}
          disabled={shutdownBusy}
          aria-label="shutdown backend"
          title="Operator-initiated clean shutdown (operator_quit intent — supervisor will not respawn)"
          data-testid="autonomy-shutdown"
        >
          {shutdownLabel}
        </button>
      </header>

      <RuntimeSection />

      <div className="autonomy-grid">
        <div className="autonomy-grid__col autonomy-grid__col--primary">
          <AgendaPane items={agenda.data} status={agenda.status} error={agenda.error} />
          <WorkersPane workers={workers.data} status={workers.status} error={workers.error} />
          <DecisionLogPane items={agenda.data} lastTick={governor.data?.last_tick ?? null} />
        </div>
        <div className="autonomy-grid__col autonomy-grid__col--side">
          <ApprovalsPane items={agenda.data} status={agenda.status} error={agenda.error} />
          <PrunedPane />
          <JournalPane rows={journal.data} status={journal.status} error={journal.error} />
          <BlockedPane
            items={agenda.data}
            pauses={governor.data?.pauses ?? []}
            itemsStatus={agenda.status}
            governorStatus={governor.status}
            itemsError={agenda.error}
            governorError={governor.error}
          />
          <RecoveryPane summary={recoveryPayload} recoveryState={recoveryState} />
          <NotificationsPane />
        </div>
      </div>

      {selectedItem && <AgendaDetailModal item={selectedItem} />}
      <WorkerDetailModal />
    </div>
  );
}
