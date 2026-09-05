import { create } from 'zustand';
import { BACKEND_BASE } from '../lib/endpoints';

export type EventKind =
  | 'feedback_proposal'
  | 'feedback_sweep'
  | 'agent_approval'
  | 'skill_approval'
  | 'skill_refinement'
  | 'working_set_proposal'
  | 'soul_proposal'
  | 'change_proposal'
  | 'mission_reflection_proposal'
  | 'reflection_proposal'
  | 'nudge'
  | 'agent_post'
  | 'operator_post'
  | 'daily_brief'
  | 'yaml_change_proposal'
  | 'kb_merge_conflict'
  | 'clarification';

export type OperatorPostSource = 'button' | 'scratchpad' | 'voice' | 'hotkey' | 'telegram';

export type EventStatus = 'pending' | 'approved' | 'rejected' | 'resolved' | 'applied' | 'deleted';
export type Author = 'operator' | 'agent';

export interface WorkspaceComment {
  comment_id: string;
  event_id: string;
  ts: string;
  author: Author;
  body: string;
  reply_to: string | null;
  delivered_to_agent: boolean;
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
  /** Resolves `true` when the decision settled (applied, or the row was
   *  already gone) and `false` when it did not — a 5xx, a network drop, a
   *  parse failure. It reports rather than throws because a caller that only
   *  wants the side effect should not have to catch; the bulk caller needs
   *  the answer, and inferring it from a rejection is what a swallowing
   *  `catch` makes impossible. `lastError` still carries the message. */
  decide: (event_id: string, decision: 'approve' | 'reject' | 'resolve' | 'delete', reason?: string) => Promise<boolean>;
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
  // What the route said went wrong. One status covers several refusals here,
  // so the code alone cannot decide what to tell the operator.
  reason: string;
  constructor(path: string, status: number, reason = '') {
    super(`${path} ${status}`);
    this.status = status;
    this.reason = reason;
  }
}

async function failure(path: string, r: Response): Promise<HttpError> {
  const body = (await r.json().catch(() => ({}))) as { error?: unknown };
  return new HttpError(path, r.status, typeof body.error === 'string' ? body.error : '');
}

async function jget<T>(path: string): Promise<T> {
  const r = await fetch(`${BACKEND_BASE}${path}`);
  if (!r.ok) throw await failure(path, r);
  return r.json() as Promise<T>;
}

async function jpost<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(`${BACKEND_BASE}${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw await failure(path, r);
  return r.json() as Promise<T>;
}

function upsert(list: WorkspaceEvent[], event: WorkspaceEvent): WorkspaceEvent[] {
  const idx = list.findIndex((e) => e.event_id === event.event_id);
  if (idx < 0) return [event, ...list];
  const next = list.slice();
  next[idx] = event;
  return next;
}

/** Put one event on the side its status belongs to.
 *
 * `events` is the pending inbox and `history` is everything decided, so a row
 * whose status just changed has to move rather than be written where it was.
 * Deciding a row used to drop it from the inbox and the file watcher then
 * broadcast the same row back a quarter of a second later, which put it
 * straight back into the pending list: the operator saw an approved row
 * reappear, the badge kept counting it, and only a reload sorted it out.
 */
function place(
  state: { events: WorkspaceEvent[]; history: WorkspaceEvent[] },
  event: WorkspaceEvent,
): { events: WorkspaceEvent[]; history: WorkspaceEvent[] } {
  if (event.status === 'pending') {
    return {
      events: upsert(state.events, event),
      history: state.history.filter((e) => e.event_id !== event.event_id),
    };
  }
  return {
    events: state.events.filter((e) => e.event_id !== event.event_id),
    history: upsert(state.history, event),
  };
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

  upsertEvent: (event) => set((s) => place(s, event)),

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
    // Belt-and-suspenders: an assistant reply landing on the thread also clears
    // the `thinking…` indicator. Server fires `cleared` in `_run_turn`'s
    // finally, but a dropped envelope shouldn't leave the row spinning.
    if (comment.author === 'agent') {
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
      set((s) => place(s, updated));
      return true;
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
        // Settled from the operator's side: the row is gone either way.
        return true;
      }
      const reason = err instanceof HttpError ? err.reason : '';
      if (status === 409 && reason === 'concurrent_modification') {
        // The file moved after the proposal was written. The backend has
        // rewritten the card against the file as it stands now, so pull it
        // back in: what is on screen is what a second Approve would commit.
        // Narrowed on the reason, because the other refusals that share this
        // status are about the change itself, not about the file moving.
        void get().refreshEvent(event_id);
        set({
          lastError:
            'This file changed after the proposal was written. The card now shows the change against the file as it is now. Approve again to apply it.',
        });
        return false;
      }
      // Real failure (5xx, network, parse). Keep the row in place so
      // the operator can retry, surface the error so they can read it.
      set({ lastError: (err as Error).message });
      return false;
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
