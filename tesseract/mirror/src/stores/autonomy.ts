// AU-7 S1 — Autonomy Dashboard store.
//
// Single source for the AutonomyView shell + its 7 read-only panes.
// Hydrates from four REST endpoints:
//
//   GET /api/agenda           — full agenda list (active items, ranked)
//   GET /api/workers/active   — every record under workers/active/
//   GET /api/governor/state   — running flag, config, last tick, pauses
//   GET /api/recovery/latest  — last RecoveryManager pass
//
// `fetchAll()` runs each fetch independently so one failing endpoint
// does not blank the dashboard. Errors are stored per-section so the
// pane can render a small inline notice instead of disappearing.
//
// Live WS updates are wired through `applyEnvelope`. AU-5/AU-6 backend
// today emits the recovery_summary workspace event; the other envelopes
// (agenda_item_*, worker_record_*, governor_pause_*) are wired here
// in S1 ready-to-receive so AU-7 S2 / later phases can emit them
// without another frontend change. Unknown types are dropped silently.

import { create } from 'zustand';
import {
  ApiError,
  fetchActiveWorkers,
  fetchAgenda,
  fetchAgendaComments,
  fetchGovernorState,
  fetchLatestRecovery,
  fetchOperatorJournal,
  fetchPruned,
  fetchWorkerDetail,
  patchAgendaItem,
  postAgendaComment,
  postApproveAgendaItem,
  postCancelAgendaItem,
  postMuteSource,
  postResumeAgendaItem,
  postRuntimeShutdown,
  postUnmuteSource,
  postUnpauseSource,
  type ActiveWorker,
  type AgendaComment,
  type AgendaItem,
  type GovernorPause,
  type GovernorStateResponse,
  type LatestRecoveryResponse,
  type OperatorJournalRow,
  type PrunedResponse,
  type WorkerDetail,
} from '../lib/api';
import type { Envelope } from '../lib/types';
import { useToastStore } from './toasts';
import { useWebSocketStore } from './websocket';

type AsyncStatus = 'idle' | 'loading' | 'ready' | 'error';

interface SectionState<T> {
  data: T;
  status: AsyncStatus;
  error: string | null;
  lastFetched: number | null;
}

function _section<T>(initial: T): SectionState<T> {
  return { data: initial, status: 'idle', error: null, lastFetched: null };
}

interface AutonomyState {
  agenda: SectionState<AgendaItem[]>;
  workers: SectionState<ActiveWorker[]>;
  governor: SectionState<GovernorStateResponse | null>;
  recovery: SectionState<LatestRecoveryResponse | null>;
  pendingActions: Set<string>;
  selectedAgendaId: string | null;
  openDetail: (id: string) => void;
  closeDetail: () => void;

  // Worker detail drawer state. Independent of agenda detail so the
  // operator can open a worker without losing an agenda modal stack.
  selectedWorkerId: string | null;
  workerDetail: WorkerDetail | null;
  workerDetailStatus: AsyncStatus;
  workerDetailError: string | null;
  openWorkerDetail: (id: string) => Promise<void>;
  closeWorkerDetail: () => void;

  // Agenda comment threads. Keyed by item id so multiple modals can
  // each fetch their own thread without trampling each other; the
  // detail modal subscribes to `agendaComments[item.id]` only.
  agendaComments: Record<string, AgendaComment[]>;
  agendaCommentStatus: Record<string, AsyncStatus>;
  agendaCommentError: Record<string, string | null>;
  fetchAgendaComments: (id: string) => Promise<void>;
  postAgendaComment: (id: string, body: string) => Promise<boolean>;

  fetchAll: () => Promise<void>;
  fetchAgenda: () => Promise<void>;
  fetchWorkers: () => Promise<void>;
  fetchGovernor: () => Promise<void>;
  fetchRecovery: () => Promise<void>;
  fetchJournal: () => Promise<void>;
  journal: SectionState<OperatorJournalRow[]>;
  applyEnvelope: (env: Envelope) => void;

  // AU-7 Phase 3 — pruned ledger (admission-gate discards, by source ×
  // stage) + per-source mute. Self-fetched by PrunedPane rather than
  // fanned out from fetchAll — it isn't one of the six GOVERNANCE §15
  // dashboard sections.
  pruned: PrunedResponse | null;
  prunedStatus: 'idle' | 'loading' | 'error';
  loadPruned: (windowHours?: number) => Promise<void>;
  muteSource: (source: string, muted: boolean) => Promise<boolean>;

  // AU-7 S2 actions. Each handles its own session_id resolution +
  // toast error path so callers only need (id, ...args).
  approveItem: (id: string) => Promise<boolean>;
  resumeItem: (id: string) => Promise<boolean>;
  cancelItem: (id: string, reason?: string) => Promise<boolean>;
  snoozeItem: (id: string) => Promise<boolean>;
  boostItem: (id: string) => Promise<boolean>;
  unpauseSource: (source: string) => Promise<boolean>;
  runtimeShutdown: (reason?: string) => Promise<boolean>;
}

const SNOOZE_PRIORITY = -2;
const BOOST_PRIORITY = 5;

function _describeError(err: unknown): string {
  if (err instanceof Error) return err.message;
  return String(err);
}

function _resolveSession(): string | null {
  const sid = useWebSocketStore.getState().sessionId;
  if (!sid) {
    useToastStore.getState().push('No session id — connect first.', 'error');
    return null;
  }
  return sid;
}

function _toastApiError(prefix: string, err: unknown): void {
  const detail = err instanceof ApiError ? err.message : _describeError(err);
  useToastStore.getState().push(`${prefix}: ${detail}`, 'error');
}

function _markPending(
  set: (fn: (s: AutonomyState) => Partial<AutonomyState>) => void,
  id: string,
  on: boolean,
): void {
  set((s) => {
    const next = new Set(s.pendingActions);
    if (on) next.add(id);
    else next.delete(id);
    return { pendingActions: next };
  });
}

export const useAutonomyStore = create<AutonomyState>((set, get) => ({
  agenda: _section<AgendaItem[]>([]),
  workers: _section<ActiveWorker[]>([]),
  governor: _section<GovernorStateResponse | null>(null),
  recovery: _section<LatestRecoveryResponse | null>(null),
  journal: _section<OperatorJournalRow[]>([]),
  pruned: null,
  prunedStatus: 'idle',
  pendingActions: new Set<string>(),
  selectedAgendaId: null,
  openDetail: (id) => set({ selectedAgendaId: id }),
  closeDetail: () => set({ selectedAgendaId: null }),

  selectedWorkerId: null,
  workerDetail: null,
  workerDetailStatus: 'idle',
  workerDetailError: null,
  openWorkerDetail: async (id) => {
    set({
      selectedWorkerId: id,
      workerDetail: null,
      workerDetailStatus: 'loading',
      workerDetailError: null,
    });
    try {
      const res = await fetchWorkerDetail(id);
      // Late-resolved fetch must not stomp a newer open or a close.
      if (get().selectedWorkerId !== id) return;
      set({
        workerDetail: res.worker,
        workerDetailStatus: 'ready',
        workerDetailError: null,
      });
    } catch (err) {
      if (get().selectedWorkerId !== id) return;
      set({
        workerDetailStatus: 'error',
        workerDetailError: _describeError(err),
      });
    }
  },
  closeWorkerDetail: () =>
    set({
      selectedWorkerId: null,
      workerDetail: null,
      workerDetailStatus: 'idle',
      workerDetailError: null,
    }),

  agendaComments: {},
  agendaCommentStatus: {},
  agendaCommentError: {},
  fetchAgendaComments: async (id) => {
    set((s) => ({
      agendaCommentStatus: { ...s.agendaCommentStatus, [id]: 'loading' },
      agendaCommentError: { ...s.agendaCommentError, [id]: null },
    }));
    try {
      const res = await fetchAgendaComments(id);
      set((s) => ({
        agendaComments: { ...s.agendaComments, [id]: res.comments },
        agendaCommentStatus: { ...s.agendaCommentStatus, [id]: 'ready' },
        agendaCommentError: { ...s.agendaCommentError, [id]: null },
      }));
    } catch (err) {
      set((s) => ({
        agendaCommentStatus: { ...s.agendaCommentStatus, [id]: 'error' },
        agendaCommentError: { ...s.agendaCommentError, [id]: _describeError(err) },
      }));
    }
  },
  postAgendaComment: async (id, body) => {
    const sid = _resolveSession();
    if (!sid) return false;
    try {
      const res = await postAgendaComment(id, { session_id: sid, body });
      // Optimistic append — the WS broadcast may also append; we dedupe
      // by comment id below in applyEnvelope so the double-emit is safe.
      set((s) => ({
        agendaComments: {
          ...s.agendaComments,
          [id]: [...(s.agendaComments[id] ?? []), res.comment],
        },
      }));
      return true;
    } catch (err) {
      _toastApiError('Post comment failed', err);
      return false;
    }
  },

  fetchAll: async () => {
    const { fetchAgenda, fetchWorkers, fetchGovernor, fetchRecovery, fetchJournal } = get();
    // Each section tracks its own load state; settle independently so
    // one slow / down endpoint doesn't gate the others.
    await Promise.allSettled([
      fetchAgenda(),
      fetchWorkers(),
      fetchGovernor(),
      fetchRecovery(),
      fetchJournal(),
    ]);
  },

  fetchAgenda: async () => {
    set((s) => ({ agenda: { ...s.agenda, status: 'loading', error: null } }));
    try {
      const res = await fetchAgenda();
      set({
        agenda: {
          data: res.items,
          status: 'ready',
          error: null,
          lastFetched: Date.now(),
        },
      });
    } catch (err) {
      set((s) => ({
        agenda: { ...s.agenda, status: 'error', error: _describeError(err) },
      }));
    }
  },

  fetchWorkers: async () => {
    set((s) => ({ workers: { ...s.workers, status: 'loading', error: null } }));
    try {
      const res = await fetchActiveWorkers();
      set({
        workers: {
          data: res.workers,
          status: 'ready',
          error: null,
          lastFetched: Date.now(),
        },
      });
    } catch (err) {
      set((s) => ({
        workers: { ...s.workers, status: 'error', error: _describeError(err) },
      }));
    }
  },

  fetchGovernor: async () => {
    set((s) => ({
      governor: { ...s.governor, status: 'loading', error: null },
    }));
    try {
      const res = await fetchGovernorState();
      set({
        governor: {
          data: res,
          status: 'ready',
          error: null,
          lastFetched: Date.now(),
        },
      });
    } catch (err) {
      set((s) => ({
        governor: { ...s.governor, status: 'error', error: _describeError(err) },
      }));
    }
  },

  fetchRecovery: async () => {
    set((s) => ({
      recovery: { ...s.recovery, status: 'loading', error: null },
    }));
    try {
      const res = await fetchLatestRecovery();
      set({
        recovery: {
          data: res,
          status: 'ready',
          error: null,
          lastFetched: Date.now(),
        },
      });
    } catch (err) {
      set((s) => ({
        recovery: { ...s.recovery, status: 'error', error: _describeError(err) },
      }));
    }
  },

  fetchJournal: async () => {
    set((s) => ({
      journal: { ...s.journal, status: 'loading', error: null },
    }));
    try {
      const res = await fetchOperatorJournal(50);
      set({
        journal: {
          data: res.rows,
          status: 'ready',
          error: null,
          lastFetched: Date.now(),
        },
      });
    } catch (err) {
      set((s) => ({
        journal: { ...s.journal, status: 'error', error: _describeError(err) },
      }));
    }
  },

  applyEnvelope: (env: Envelope) => {
    // S1: future-ready dispatcher. Each branch is conservative — if the
    // payload shape doesn't match expectations, the envelope is dropped
    // and the next poll repairs state. Cheaper than blocking renders on
    // strict schema validation for envelopes that may still be evolving.
    //
    // recovery_summary rides the workspace channel as
    // ``workspace_event_appended`` with ``data.kind === 'recovery_summary'``
    // (see app.py::_run_recovery). Everything else is direct-typed.
    if (env.type === 'workspace_event_appended') {
      const kind = (env.data as { kind?: unknown })?.kind;
      if (kind === 'recovery_summary') {
        void get().fetchRecovery();
      }
      return;
    }
    switch (env.type) {
      case 'agenda_item_added':
      case 'agenda_item_transitioned':
      case 'agenda_item_updated': {
        void get().fetchAgenda();
        return;
      }
      case 'agenda_comment_added': {
        const data = env.data as { item_id?: string; comment?: AgendaComment };
        if (!data?.item_id || !data?.comment) return;
        const { item_id, comment } = data;
        set((s) => {
          const current = s.agendaComments[item_id] ?? [];
          // Dedupe: optimistic-append + WS broadcast both fire after a
          // local post. Skip if we already have this id.
          if (current.some((c) => c.id === comment.id)) return s;
          return {
            agendaComments: {
              ...s.agendaComments,
              [item_id]: [...current, comment],
            },
          };
        });
        return;
      }
      case 'worker_record_started':
      case 'worker_record_transitioned':
      case 'worker_record_archived': {
        void get().fetchWorkers();
        return;
      }
      case 'governor_pause_added':
      case 'governor_pause_removed':
      case 'governor_tick': {
        void get().fetchGovernor();
        return;
      }
      default:
        // Not for us — silent drop is correct; the dispatch.ts log
        // covers genuinely-unrouted envelopes.
        return;
    }
  },

  // -- AU-7 S2 actions ----------------------------------------------

  approveItem: async (id) => {
    const sid = _resolveSession();
    if (!sid) return false;
    _markPending(set, id, true);
    try {
      await postApproveAgendaItem(id, { session_id: sid });
      useToastStore.getState().push('Approved', 'info');
      await get().fetchAgenda();
      return true;
    } catch (err) {
      _toastApiError('Approve failed', err);
      return false;
    } finally {
      _markPending(set, id, false);
    }
  },

  resumeItem: async (id) => {
    const sid = _resolveSession();
    if (!sid) return false;
    _markPending(set, id, true);
    try {
      const res = await postResumeAgendaItem(id, { session_id: sid });
      useToastStore.getState().push(
        res.noop ? 'Already past blocked — no resume needed' : 'Re-queued for next tick',
        'info',
      );
      await get().fetchAgenda();
      return true;
    } catch (err) {
      _toastApiError('Resume failed', err);
      return false;
    } finally {
      _markPending(set, id, false);
    }
  },

  cancelItem: async (id, reason) => {
    const sid = _resolveSession();
    if (!sid) return false;
    _markPending(set, id, true);
    try {
      await postCancelAgendaItem(id, {
        session_id: sid,
        reason: reason ?? 'operator_cancel',
      });
      useToastStore.getState().push('Cancelled', 'info');
      await get().fetchAgenda();
      return true;
    } catch (err) {
      _toastApiError('Cancel failed', err);
      return false;
    } finally {
      _markPending(set, id, false);
    }
  },

  snoozeItem: async (id) => {
    const sid = _resolveSession();
    if (!sid) return false;
    _markPending(set, id, true);
    try {
      await patchAgendaItem(id, {
        session_id: sid,
        operator_priority: SNOOZE_PRIORITY,
      });
      useToastStore.getState().push('Snoozed', 'info');
      await get().fetchAgenda();
      return true;
    } catch (err) {
      _toastApiError('Snooze failed', err);
      return false;
    } finally {
      _markPending(set, id, false);
    }
  },

  boostItem: async (id) => {
    const sid = _resolveSession();
    if (!sid) return false;
    _markPending(set, id, true);
    try {
      await patchAgendaItem(id, {
        session_id: sid,
        operator_priority: BOOST_PRIORITY,
      });
      useToastStore.getState().push('Boosted', 'info');
      await get().fetchAgenda();
      return true;
    } catch (err) {
      _toastApiError('Boost failed', err);
      return false;
    } finally {
      _markPending(set, id, false);
    }
  },

  unpauseSource: async (source) => {
    const sid = _resolveSession();
    if (!sid) return false;
    const key = `pause:${source}`;
    _markPending(set, key, true);
    try {
      const res = await postUnpauseSource(source, { session_id: sid });
      useToastStore.getState().push(
        res.was_paused ? `Unpaused ${source}` : `${source} was not paused`,
        'info',
      );
      await get().fetchGovernor();
      return true;
    } catch (err) {
      _toastApiError(`Unpause ${source} failed`, err);
      return false;
    } finally {
      _markPending(set, key, false);
    }
  },

  runtimeShutdown: async (reason) => {
    const sid = _resolveSession();
    if (!sid) return false;
    const key = 'runtime:shutdown';
    _markPending(set, key, true);
    try {
      await postRuntimeShutdown({
        session_id: sid,
        reason: reason ?? 'operator clicked shutdown',
      });
      useToastStore.getState().push('Shutting down…', 'warning');
      return true;
    } catch (err) {
      _toastApiError('Shutdown failed', err);
      return false;
    } finally {
      _markPending(set, key, false);
    }
  },

  // -- AU-7 Phase 3 — pruned ledger + mute ---------------------------

  loadPruned: async (windowHours) => {
    set({ prunedStatus: 'loading' });
    try {
      const res = await fetchPruned(windowHours);
      set({ pruned: res, prunedStatus: 'idle' });
    } catch (err) {
      set({ prunedStatus: 'error' });
      _toastApiError('Load pruned failed', err);
    }
  },

  muteSource: async (source, muted) => {
    const sid = _resolveSession();
    if (!sid) return false;
    const key = `prune-mute:${source}`;
    _markPending(set, key, true);
    try {
      if (muted) {
        await postMuteSource(source, { session_id: sid });
      } else {
        await postUnmuteSource(source, { session_id: sid });
      }
      useToastStore.getState().push(muted ? `Muted ${source}` : `Unmuted ${source}`, 'info');
      await Promise.allSettled([get().loadPruned(), get().fetchAgenda()]);
      return true;
    } catch (err) {
      _toastApiError(`${muted ? 'Mute' : 'Unmute'} ${source} failed`, err);
      return false;
    } finally {
      _markPending(set, key, false);
    }
  },
}));

export function selectPauseBySource(
  state: AutonomyState,
  source: string,
): GovernorPause | undefined {
  return state.governor.data?.pauses.find((p) => p.source === source);
}

export function selectAwaitingOperator(state: AutonomyState): AgendaItem[] {
  return state.agenda.data.filter((i) => i.status === 'awaiting_operator');
}

export function selectBlocked(state: AutonomyState): AgendaItem[] {
  return state.agenda.data.filter((i) => i.status === 'blocked');
}

export function selectRunning(state: AutonomyState): AgendaItem[] {
  return state.agenda.data.filter(
    (i) => i.status === 'running' || i.status === 'selected',
  );
}
