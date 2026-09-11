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
  fetchHealth,
  fetchAutonomyAtlas,
  fetchAutonomyHistory,
  fetchAutonomyRetention,
  fetchAutonomyChannels,
  fetchAutonomyDay,
  fetchAutonomyMemory,
  fetchManaged,
  fetchMachineMap,
  fetchRoomLines,
  fetchOperatorJournal,
  fetchOverview,
  fetchReturnNote,
  fetchPipeline,
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
  type OverviewResponse,
  type ReturnNoteResponse,
  type HealthResponse,
  type ChannelsResponse,
  type DayResponse,
  type AtlasResponse,
  type HistoryResponse,
  type RetentionResponse,
  type MemoryResponse,
  type ManagedResponse,
  type MachineMapResponse,
  type PipelineResponse,
  type RoomLinesResponse,
  type PrunedResponse,
  type WorkerDetail,
} from '../lib/api';
import type { Envelope } from '../lib/types';
import { useToastStore } from './toasts';
import { useWebSocketStore } from './websocket';

type AsyncStatus = 'idle' | 'loading' | 'ready' | 'error';

/** One step down inside a room. `kind` says what the pane should render and
 *  `id` says which one; the room owns the rendering, this owns the trail. */
export interface AutonomyLevel {
  kind: 'entry' | 'agent' | 'agenda' | 'worker' | 'step' | 'effect';
  /** The record's own key, never the label a row happened to render. An
   *  `effect` carries the call's id and not the clarification card's: the
   *  card's id is derived from it in one place, the way the backend derives
   *  it in one place. */
  id: string;
  /** What the breadcrumb calls it. */
  label: string;
}

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
  fetchHistory: () => Promise<void>;
  fetchWorkers: () => Promise<void>;
  fetchGovernor: () => Promise<void>;
  fetchRecovery: () => Promise<void>;
  fetchJournal: () => Promise<void>;
  journal: SectionState<OperatorJournalRow[]>;
  // What changed since the operator was last here. The Journal's own band,
  // fetched beside its rows so the room is one request's worth of stale
  // rather than two.
  returnNote: SectionState<ReturnNoteResponse | null>;
  fetchReturnNote: () => Promise<void>;
  // The operations strip. Fed by REST alone for now: the pipeline emits no
  // envelope of its own yet, so the strip decides it is stale from the age of
  // its own last fetch rather than from a socket it does not listen to.
  pipeline: SectionState<PipelineResponse | null>;
  fetchPipeline: () => Promise<void>;

  // The floor plan's map. Declared shape from the backend, and state on the
  // nodes that have a producer. REST like the strip, and for the same reason:
  // nothing broadcasts a node's state yet.
  map: SectionState<MachineMapResponse | null>;
  fetchMap: () => Promise<void>;

  // The Health room's one feed. REST like the strip and the map: the watchman
  // writes its sweep every quarter of an hour and nothing broadcasts one, so
  // what this can honestly detect is its own payload going stale.
  health: SectionState<HealthResponse | null>;
  fetchHealth: () => Promise<void>;

  // Managed system: the scheduled rows, the agents and the alarms, each split
  // by who wrote it. REST for the same reason as the rest of this panel, and
  // re-read after every action rather than patched here: what a row's state
  // means is the backend's answer, and deciding it after a click is how a
  // second definition of it starts.
  managed: SectionState<ManagedResponse | null>;
  fetchManaged: () => Promise<void>;

  // Memory: the library, what the last pass changed in it, and whether it
  // can be searched. Held here rather than fetched by the room, because the
  // rail's mark says whether retrieval is impaired whether or not it is open.
  memory: SectionState<MemoryResponse | null>;
  fetchMemory: () => Promise<void>;

  // Channels: whether anything the app writes can reach the operator. Held
  // here rather than fetched by the room, because the rail's own band carries
  // what it last sent whether or not the room is open.
  channels: SectionState<ChannelsResponse | null>;
  fetchChannels: () => Promise<void>;

  // The day: what it decided this morning and has carried forward since.
  // Held here like the rest, because the rail draws the room's mark whether
  // or not the room is open.
  day: SectionState<DayResponse | null>;
  fetchDay: () => Promise<void>;

  // Atlas: whether the map of how everything connects is current, and what
  // the last pass over it found. Held here for the same reason as the rest:
  // the rail carries the room's mark whether or not the room is open.
  atlas: SectionState<AtlasResponse | null>;
  fetchAtlas: () => Promise<void>;

  // What it throws away: the trees with a window, the ones kept on purpose,
  // and the ones nothing has decided about. Held here for the same reason as
  // the rest: the rail carries the room's mark whether or not it is open.
  retention: SectionState<RetentionResponse | null>;
  /** Two weeks of the three numbers Health is asked about.
   *
   *  Deliberately NOT in `fetchAll`: no rail mark reads it, so a panel
   *  load that skipped it costs nothing, and it opens a few megabytes of
   *  log. The Health room asks for it when it is opened. */
  history: SectionState<HistoryResponse | null>;
  fetchRetention: () => Promise<void>;

  // When the scheduler last told us it had changed a row. An open entry card
  // watches this so a cadence it just set is re-read from the backend rather
  // than assumed: the command answers with an envelope, not with a response,
  // so there is nothing to await at the point of the click.
  scheduleTouchedAt: number;
  markScheduleTouched: () => void;

  // Overview: what wants the operator, what is working, and what ran while
  // they were away. The join behind the first band is the backend's, so this
  // holds a payload rather than assembling one.
  overview: SectionState<OverviewResponse | null>;
  fetchOverview: () => Promise<void>;

  // The rail's written lines. One feed, keyed by room, and the frontend
  // authors none of it: a room described in TSX is a room whose description
  // goes stale on its own.
  roomLines: SectionState<RoomLinesResponse | null>;
  fetchRoomLines: () => Promise<void>;
  /** Everything the Managed system room shows: its rows AND its sentence.
   *  Four callers wanted both and three of them asked for only the rows, so
   *  the room header went on describing the state before the change. What a
   *  re-read of this room IS belongs here, not in each caller's memory. */
  rereadManagedRoom: () => Promise<void>;

  // Where the operator is inside the open room. Level 0 is the room itself;
  // every push renders INSIDE the pane rather than over it, which is the whole
  // point: a floor plan you cannot see past has stopped being one.
  //
  // The stack is cleared when the rail changes room, because a crumb trail
  // that survives into a different room names a place you are no longer in.
  levels: AutonomyLevel[];
  pushLevel: (level: AutonomyLevel) => void;
  /** Return to `index`, dropping everything below it. */
  goToLevel: (index: number) => void;
  clearLevels: () => void;
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
    useToastStore.getState().push('No session id. Connect first.', 'error');
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
  returnNote: _section<ReturnNoteResponse | null>(null),
  pipeline: _section<PipelineResponse | null>(null),
  map: _section<MachineMapResponse | null>(null),
  health: _section<HealthResponse | null>(null),
  managed: _section<ManagedResponse | null>(null),
  memory: _section<MemoryResponse | null>(null),
  channels: _section<ChannelsResponse | null>(null),
  day: _section<DayResponse | null>(null),
  atlas: _section<AtlasResponse | null>(null),
  retention: _section<RetentionResponse | null>(null),
  history: _section<HistoryResponse | null>(null),
  scheduleTouchedAt: 0,
  overview: _section<OverviewResponse | null>(null),
  roomLines: _section<RoomLinesResponse | null>(null),
  levels: [],
  pruned: null,
  prunedStatus: 'idle',
  pendingActions: new Set<string>(),
  selectedAgendaId: null,
  // Opening an item is a LEVEL, not a modal. Every pane that used to raise a
  // card over itself calls this, so they all moved together.
  openDetail: (id) => {
    const item = get().agenda.data.find((i) => i.id === id);
    set({ selectedAgendaId: id });
    get().pushLevel({ kind: 'agenda', id, label: item?.goal ?? id });
  },
  closeDetail: () => set({ selectedAgendaId: null }),

  selectedWorkerId: null,
  workerDetail: null,
  workerDetailStatus: 'idle',
  workerDetailError: null,
  openWorkerDetail: async (id) => {
    const known = get().workers.data.find((w) => w.id === id);
    get().pushLevel({
      kind: 'worker',
      id,
      label: known?.role || known?.kind || id,
    });
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
    const {
      fetchAgenda,
      fetchWorkers,
      fetchGovernor,
      fetchRecovery,
      fetchJournal,
      fetchReturnNote,
      fetchPipeline,
      fetchHealth,
      fetchManaged,
      fetchMemory,
      fetchChannels,
      fetchDay,
      fetchAtlas,
      fetchRetention,
      fetchOverview,
      fetchRoomLines,
      loadPruned,
    } = get();
    // Each section tracks its own load state; settle independently so
    // one slow / down endpoint doesn't gate the others.
    //
    // Health is here rather than in its own room alone: the rail carries the
    // room's line whether or not the room is open, and a rail that only knows
    // what it is showing has stopped being the overview.
    await Promise.allSettled([
      fetchAgenda(),
      fetchWorkers(),
      fetchGovernor(),
      fetchRecovery(),
      fetchJournal(),
      fetchReturnNote(),
      fetchPipeline(),
      fetchHealth(),
      fetchManaged(),
      // The day, because the rail's Today line is drawn from it whether or
      // not the room is open, the same rule every other section here follows.
      fetchDay(),
      // The rail says whether anything can reach the operator, which is
      // the one fact on this panel that is about the panel being read at
      // all. It is not waited for by the room.
      fetchMemory(),
      fetchChannels(),
      fetchAtlas(),
      fetchRetention(),
      // The room the panel opens on, and the room whose mark the rail draws
      // first. Both need it before anything is clicked.
      fetchOverview(),
      // The rail carries a mark for Managed system whether or not the room is
      // open, and `rooms.ts` reads it from this data: without it every load
      // drew the no-producer mark on a room with a perfectly good producer.
      fetchRoomLines(),
      // The rail carries a line for every room whether or not it is open, and
      // a room whose data nothing fetched cannot say what is in it.
      loadPruned(),
    ]);
  },

  pushLevel: (level) =>
    set((s) => {
      const top = s.levels[s.levels.length - 1];
      // Selecting the thing you are already looking at is not a new level.
      if (top && top.kind === level.kind && top.id === level.id) return {};
      // Neither is selecting a SIBLING of it. Opening one entry and then
      // another from the wiring beside it is still one thing open, at the same
      // depth, and stacking them made the trail read
      // `Managed system > capture > watchman > capture > consolidate > ...`,
      // which names no place anybody is. Only the top level renders, so every
      // crumb under it was unreachable furniture. A level of the same KIND
      // replaces the one it is a sibling of; a different kind is a real
      // descent and pushes.
      if (top && top.kind === level.kind) {
        return { levels: [...s.levels.slice(0, -1), level] };
      }
      return { levels: [...s.levels, level] };
    }),
  goToLevel: (index) => set((s) => ({ levels: s.levels.slice(0, index) })),
  clearLevels: () => set({ levels: [] }),

  fetchMap: async () => {
    set((s) => ({ map: { ...s.map, status: 'loading', error: null } }));
    try {
      const res = await fetchMachineMap();
      set({ map: { data: res, status: 'ready', error: null, lastFetched: Date.now() } });
    } catch (err) {
      set((s) => ({ map: { ...s.map, status: 'error', error: _describeError(err) } }));
    }
  },

  fetchHealth: async () => {
    set((s) => ({ health: { ...s.health, status: 'loading', error: null } }));
    try {
      const res = await fetchHealth();
      set({
        health: { data: res, status: 'ready', error: null, lastFetched: Date.now() },
      });
    } catch (err) {
      set((s) => ({
        health: { ...s.health, status: 'error', error: _describeError(err) },
      }));
    }
  },

  fetchMemory: async () => {
    set((s) => ({ memory: { ...s.memory, status: 'loading', error: null } }));
    try {
      const res = await fetchAutonomyMemory();
      set({
        memory: { data: res, status: 'ready', error: null, lastFetched: Date.now() },
      });
    } catch (err) {
      set((s) => ({
        memory: { ...s.memory, status: 'error', error: _describeError(err) },
      }));
    }
  },

  fetchAtlas: async () => {
    set((s) => ({ atlas: { ...s.atlas, status: 'loading', error: null } }));
    try {
      const res = await fetchAutonomyAtlas();
      set({
        atlas: { data: res, status: 'ready', error: null, lastFetched: Date.now() },
      });
    } catch (err) {
      set((s) => ({
        atlas: { ...s.atlas, status: 'error', error: _describeError(err) },
      }));
    }
  },

  fetchRetention: async () => {
    set((s) => ({ retention: { ...s.retention, status: 'loading', error: null } }));
    try {
      const res = await fetchAutonomyRetention();
      set({
        retention: {
          data: res,
          status: 'ready',
          error: null,
          lastFetched: Date.now(),
        },
      });
    } catch (err) {
      set((s) => ({
        retention: { ...s.retention, status: 'error', error: _describeError(err) },
      }));
    }
  },

  fetchHistory: async () => {
    set((s) => ({ history: { ...s.history, status: 'loading', error: null } }));
    try {
      const res = await fetchAutonomyHistory();
      set({
        history: { data: res, status: 'ready', error: null, lastFetched: Date.now() },
      });
    } catch (err) {
      set((s) => ({
        history: { ...s.history, status: 'error', error: _describeError(err) },
      }));
    }
  },

  fetchDay: async () => {
    set((s) => ({ day: { ...s.day, status: 'loading', error: null } }));
    try {
      const res = await fetchAutonomyDay();
      set({
        day: { data: res, status: 'ready', error: null, lastFetched: Date.now() },
      });
    } catch (err) {
      set((s) => ({
        day: { ...s.day, status: 'error', error: _describeError(err) },
      }));
    }
  },

  fetchChannels: async () => {
    set((s) => ({ channels: { ...s.channels, status: 'loading', error: null } }));
    try {
      const res = await fetchAutonomyChannels();
      set({
        channels: { data: res, status: 'ready', error: null, lastFetched: Date.now() },
      });
    } catch (err) {
      set((s) => ({
        channels: { ...s.channels, status: 'error', error: _describeError(err) },
      }));
    }
  },

  markScheduleTouched: () => set({ scheduleTouchedAt: Date.now() }),

  fetchManaged: async () => {
    set((s) => ({ managed: { ...s.managed, status: 'loading', error: null } }));
    try {
      const res = await fetchManaged();
      set({
        managed: { data: res, status: 'ready', error: null, lastFetched: Date.now() },
      });
    } catch (err) {
      set((s) => ({
        managed: { ...s.managed, status: 'error', error: _describeError(err) },
      }));
    }
  },

  fetchOverview: async () => {
    set((s) => ({ overview: { ...s.overview, status: 'loading', error: null } }));
    try {
      const res = await fetchOverview();
      set({
        overview: { data: res, status: 'ready', error: null, lastFetched: Date.now() },
      });
    } catch (err) {
      set((s) => ({
        overview: { ...s.overview, status: 'error', error: _describeError(err) },
      }));
    }
  },

  rereadManagedRoom: async () => {
    const { fetchManaged, fetchRoomLines } = get();
    await Promise.all([fetchManaged(), fetchRoomLines()]);
  },

  fetchRoomLines: async () => {
    set((s) => ({ roomLines: { ...s.roomLines, status: 'loading', error: null } }));
    try {
      const res = await fetchRoomLines();
      set({
        roomLines: { data: res, status: 'ready', error: null, lastFetched: Date.now() },
      });
    } catch (err) {
      set((s) => ({
        roomLines: { ...s.roomLines, status: 'error', error: _describeError(err) },
      }));
    }
  },

  fetchPipeline: async () => {
    set((s) => ({ pipeline: { ...s.pipeline, status: 'loading', error: null } }));
    try {
      const res = await fetchPipeline();
      set({
        pipeline: {
          data: res,
          status: 'ready',
          error: null,
          lastFetched: Date.now(),
        },
      });
    } catch (err) {
      // The last payload is kept so the strip can say how old it is. It is
      // NOT rendered as current: a stale run drawn as live is the failure
      // the whole surface exists to prevent.
      set((s) => ({
        pipeline: { ...s.pipeline, status: 'error', error: _describeError(err) },
      }));
    }
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

  fetchReturnNote: async () => {
    set((s) => ({
      returnNote: { ...s.returnNote, status: 'loading', error: null },
    }));
    try {
      const res = await fetchReturnNote();
      set({
        returnNote: {
          data: res,
          status: 'ready',
          error: null,
          lastFetched: Date.now(),
        },
      });
    } catch (err) {
      set((s) => ({
        returnNote: { ...s.returnNote, status: 'error', error: _describeError(err) },
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
        res.noop ? 'Already past blocked, so no resume is needed' : 'Re-queued for next tick',
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
