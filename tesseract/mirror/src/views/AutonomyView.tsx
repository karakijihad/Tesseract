// AU-7 — Autonomy Dashboard default view.
//
// Single screen that lets the operator answer the six GOVERNANCE §15
// questions without tab clicks:
//
//   Q1 (what is the assistant doing right now?) → AgendaPane + WorkersPane
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

import { useEffect } from 'react';
import { Button } from '../components/common/Button';
import { RailView, type RailGroup } from '../components/common/RailView';
import { useAutonomyStore } from '../stores/autonomy';
import { AgendaDetailModal } from './autonomy/AgendaDetailModal';
import { AgendaPane } from './autonomy/AgendaPane';
import { ApprovalsPane } from './autonomy/ApprovalsPane';
import { BlockedPane } from './autonomy/BlockedPane';
import { DecisionLogPane } from './autonomy/DecisionLogPane';
import { JournalPane } from './autonomy/JournalPane';
import { NotificationsPane } from './autonomy/NotificationsPane';
import { PrunedPane } from './autonomy/PrunedPane';
import { RecoveryPane, type RecoverySummaryPayload } from './autonomy/RecoveryPane';
import { WorkerDetailModal } from './autonomy/WorkerDetailModal';
import { WorkersPane } from './autonomy/WorkersPane';

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

  const liveWorkerCount = workers.data.filter((w) =>
    LIVE_WORKER_STATUSES.has(w.status),
  ).length;

  const groups: RailGroup[] = [
    {
      label: 'Now',
      sections: [
        {
          key: 'overview',
          label: 'Overview',
          // The dashboard stays one screen: AU-7 exists so the six governance
          // questions are answered without clicking, and splitting it into
          // six rail rows would undo exactly that. The rail carries the long
          // tail that used to compete with it for the same column.
          render: () => (
            <div className="autonomy-grid">
              <div className="autonomy-grid__col autonomy-grid__col--primary">
                <AgendaPane items={agenda.data} status={agenda.status} error={agenda.error} />
                <WorkersPane workers={workers.data} status={workers.status} error={workers.error} />
                <DecisionLogPane items={agenda.data} lastTick={governor.data?.last_tick ?? null} />
              </div>
              <div className="autonomy-grid__col autonomy-grid__col--side">
                <ApprovalsPane items={agenda.data} status={agenda.status} error={agenda.error} />
                <RecoveryPane summary={recoveryPayload} recoveryState={recoveryState} />
              </div>
            </div>
          ),
        },
        {
          key: 'blocked',
          label: 'Blocked & paused',
          render: () => (
            <BlockedPane
              items={agenda.data}
              pauses={governor.data?.pauses ?? []}
              itemsStatus={agenda.status}
              governorStatus={governor.status}
              itemsError={agenda.error}
              governorError={governor.error}
            />
          ),
        },
      ],
    },
    {
      label: 'Record',
      sections: [
        {
          key: 'journal',
          label: 'Journal',
          title: 'Operator journal',
          render: () => (
            <JournalPane rows={journal.data} status={journal.status} error={journal.error} />
          ),
        },
        { key: 'pruned', label: 'Pruned', render: () => <PrunedPane /> },
        {
          key: 'notifications',
          label: 'Notifications',
          render: () => <NotificationsPane />,
        },
      ],
    },
  ];

  return (
    <div className="autonomy-view" data-testid="autonomy-view">
      <RailView
        groups={groups}
        label="Autonomy sections"
        meta={
          <>
            {governor.data?.running ? 'governor running' : 'governor offline'}
            {' · '}
            {liveWorkerCount} worker{liveWorkerCount === 1 ? '' : 's'}
            {' · '}
            {agenda.data.length} agenda
          </>
        }
        actions={
          <Button onClick={() => void fetchAll()} ariaLabel="refresh autonomy state">
            refresh
          </Button>
        }
      />

      {selectedItem && <AgendaDetailModal item={selectedItem} />}
      <WorkerDetailModal />
    </div>
  );
}
