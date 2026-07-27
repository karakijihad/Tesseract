import { create } from 'zustand';
import { BACKEND_BASE } from '../lib/endpoints';

export type EventKind =
  | 'feedback_proposal'
  | 'feedback_sweep'
  | 'agent_approval'
  | 'skill_approval'
  | 'skill_refinement'
  | 'soul_proposal'
  | 'change_proposal'
  | 'mission_reflection_proposal'
  | 'reflection_proposal'
  | 'nudge'
  | 'tars_post'
  | 'operator_post'
  | 'daily_brief'
  | 'yaml_change_proposal'
  | 'kb_merge_conflict'
  | 'clarification';

export type OperatorPostSource = 'button' | 'scratchpad' | 'voice' | 'hotkey' | 'telegram';

export type EventStatus = 'pending' | 'approved' | 'rejected' | 'resolved' | 'applied' | 'deleted';
export type Author = 'operator' | 'tars';

export interface WorkspaceComment {
  comment_id: string;
  event_id: string;
  ts: string;
  author: Author;
  body: string;
  reply_to: string | null;
  delivered_to_tars: boolean;
}

export interface WorkspaceEvent {
  event_id: string;
  ts: string;
  kind: EventKind;
  source: string;
  title: string;
  summary: string;
  payload: Record<string, unknown>;
  status: EventStatus;
  priority: number;
  decided_at: string | null;
  decided_reason: string | null;
  author_id: string;
  author_display: string;
  comments: WorkspaceComment[];
}

interface SeenMap {
  inbox?: string;
  stream?: string;
}

export type ThreadPendingState = 'queued' | 'thinking';

export interface ThreadPendingEntry {
  comment_id: string;
  state: ThreadPendingState;
}

interface WorkspaceState {
  events: WorkspaceEvent[];
  history: WorkspaceEvent[];
  seen: SeenMap;
  loading: boolean;
  loadingHistory: boolean;
  lastError: string | null;
  pendingThreads: Record<string, ThreadPendingEntry>;
  fetchInbox: () => Promise<void>;
  fetchHistory: () => Promise<void>;
  fetchSeen: () => Promise<void>;
  upsertEvent: (event: WorkspaceEvent) => void;
  appendComment: (comment: WorkspaceComment) => void;
  setThreadPending: (event_id: string, entry: ThreadPendingEntry | null) => void;
  refreshEvent: (event_id: string) => Promise<void>;
  decide: (event_id: string, decision: 'approve' | 'reject' | 'resolve' | 'delete', reason?: string) => Promise<void>;
  channelGate: (event_id: string, action: 'approve_next_turn' | 'reject_and_message', reply?: string) => Promise<void>;
  comment: (event_id: string, body: string) => Promise<void>;
  markPanelSeen: (panel: 'inbox' | 'stream') => Promise<void>;
  unreadCount: () => number;
  attentionCount: () => number;
}

const ATTENTION_AGE_MS = 24 * 60 * 60 * 1000;

function pickNewer(local: string | undefined, remote: string | undefined): string | undefined {
  if (!local) return remote;
  if (!remote) return local;
  return local > remote ? local : remote;
}

class HttpError extends Error {
  status: number;
  constructor(path: string, status: number) {
    super(`${path} ${status}`);
    this.status = status;
  }
}

async function jget<T>(path: string): Promise<T> {
  const r = await fetch(`${BACKEND_BASE}${path}`);
  if (!r.ok) throw new HttpError(path, r.status);
  return r.json() as Promise<T>;
}

async function jpost<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${BACKEND_BASE}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new HttpError(path, r.status);
  return r.json() as Promise<T>;
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  events: [],
  history: [],
  seen: {},
  loading: false,
  loadingHistory: false,
  lastError: null,
  pendingThreads: {},

  setThreadPending: (event_id, entry) =>
    set((s) => {
      const next = { ...s.pendingThreads };
      if (entry === null) {
        delete next[event_id];
      } else {
        next[event_id] = entry;
      }
      return { pendingThreads: next };
    }),

  fetchInbox: async () => {
    set({ loading: true, lastError: null });
    try {
      const data = await jget<{ events: WorkspaceEvent[] }>(
        '/api/workspace/inbox?status=pending',
      );
      set({ events: data.events, loading: false });
    } catch (err) {
      set({ loading: false, lastError: (err as Error).message });
    }
  },

  fetchHistory: async () => {
    set({ loadingHistory: true, lastError: null });
    try {
      const data = await jget<{ events: WorkspaceEvent[] }>(
        '/api/workspace/inbox?status=all',
      );
      const decided = data.events.filter((e) => e.status !== 'pending');
      set({ history: decided, loadingHistory: false });
    } catch (err) {
      set({ loadingHistory: false, lastError: (err as Error).message });
    }
  },

  fetchSeen: async () => {
    try {
      const data = await jget<SeenMap>('/api/workspace/seen');
      // Merge: keep whichever value is newer per panel. An optimistic
      // markPanelSeen call may have set a fresher local timestamp before
      // this fetch resolved — clobbering it would flicker the unread dots.
      set((s) => ({
        seen: {
          inbox: pickNewer(s.seen.inbox, data.inbox),
          stream: pickNewer(s.seen.stream, data.stream),
        },
      }));
    } catch {
      /* swallow — seen is best-effort */
    }
  },

  upsertEvent: (event) => {
    const events = get().events.slice();
    const idx = events.findIndex((e) => e.event_id === event.event_id);
    if (idx >= 0) events[idx] = event;
    else events.unshift(event);
    set({ events });
  },

  appendComment: (comment) => {
    // Live-push from `workspace_comment_appended` envelope. Dedupe by
    // comment_id. If the parent event isn't in the in-memory list
    // (settled/filtered/never loaded), fall back to refreshEvent so
    // the broadcast isn't silently dropped — without this the operator
    // had to leave & re-enter the tab to see their own comment.
    const events = get().events.slice();
    const idx = events.findIndex((e) => e.event_id === comment.event_id);
    if (idx < 0) {
      void get().refreshEvent(comment.event_id);
      return;
    }
    const ev = events[idx];
    if (ev.comments.some((c) => c.comment_id === comment.comment_id)) return;
    events[idx] = { ...ev, comments: [...ev.comments, comment] };
    // Belt-and-suspenders: a TARS reply landing on the thread also clears
    // the `thinking…` indicator. Server fires `cleared` in `_run_turn`'s
    // finally, but a dropped envelope shouldn't leave the row spinning.
    if (comment.author === 'tars') {
      const pt = { ...get().pendingThreads };
      if (pt[comment.event_id]) {
        delete pt[comment.event_id];
        set({ events, pendingThreads: pt });
        return;
      }
    }
    set({ events });
  },

  refreshEvent: async (event_id) => {
    try {
      const ev = await jget<WorkspaceEvent>(`/api/workspace/event/${event_id}`);
      get().upsertEvent(ev);
    } catch {
      /* swallow — best-effort */
    }
  },

  decide: async (event_id, decision, reason) => {
    try {
      const updated = await jpost<WorkspaceEvent>(
        `/api/workspace/event/${event_id}/decision`,
        { decision, reason: reason ?? '' },
      );
      set((s) => ({
        events: s.events.filter((e) => e.event_id !== updated.event_id),
        history: s.history.some((e) => e.event_id === updated.event_id)
          ? s.history.map((e) => (e.event_id === updated.event_id ? updated : e))
          : s.history,
      }));
    } catch (err) {
      const status = err instanceof HttpError ? err.status : 0;
      if (status === 404) {
        // Stale row: backend purged it already. Drop locally so the
        // click registers as a removal; resync both panes silently.
        set((s) => ({
          events: s.events.filter((e) => e.event_id !== event_id),
          history: s.history.filter((e) => e.event_id !== event_id),
        }));
        void get().fetchInbox();
        void get().fetchHistory();
      } else {
        // Real failure (5xx, network, parse). Keep the row in place so
        // the operator can retry, surface the error so they can read it.
        set({ lastError: (err as Error).message });
      }
    }
  },

  channelGate: async (event_id, action, reply) => {
    try {
      const updated = await jpost<WorkspaceEvent>(
        `/api/workspace/event/${event_id}/channel-gate`,
        { action, reply: reply ?? '' },
      );
      set((s) => ({
        events: s.events.filter((e) => e.event_id !== updated.event_id),
        history: s.history.some((e) => e.event_id === updated.event_id)
          ? s.history.map((e) => (e.event_id === updated.event_id ? updated : e))
          : s.history,
      }));
    } catch (err) {
      set({ lastError: (err as Error).message });
    }
  },

  comment: async (event_id, body) => {
    await jpost(`/api/workspace/event/${event_id}/comment`, { body });
    await get().refreshEvent(event_id);
  },

  markPanelSeen: async (panel) => {
    const last_seen_at = new Date().toISOString();
    set({ seen: { ...get().seen, [panel]: last_seen_at } });
    try {
      await jpost('/api/workspace/seen', { panel, last_seen_at });
    } catch {
      /* swallow */
    }
  },

  unreadCount: () => {
    // Inbox is fetched with `?status=pending`, so `events` is already the
    // "needs operator decision" set. The previous timestamp-vs-lastSeen
    // semantic dropped the badge to 0 the moment the operator opened the
    // tab — even though the items were still awaiting action. The HUD
    // badge means "you have stuff to do here," not "things arrived since
    // your last visit," so we count the pending set directly.
    return get().events.length;
  },

  attentionCount: () => {
    // m2 (Codex 2026-05-06): operator_post now has a real `resolve`
    // verb, so the previous "skip operator_post in attention aging"
    // workaround is gone. An unresolved operator thread sitting >24h
    // is a real stuck item the operator should see.
    const now = Date.now();
    return get().events.filter((e) => {
      const t = Date.parse(e.ts);
      return Number.isFinite(t) && now - t > ATTENTION_AGE_MS;
    }).length;
  },
}));
