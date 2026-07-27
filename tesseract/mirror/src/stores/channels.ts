/* MO-9-11 Channels store — Status + Logs panes.
 * MO-9-12 extends: Users + Conversations panes.
 *
 * Wraps:
 *   GET    /api/channels
 *   GET    /api/channels/{name}/users
 *   GET    /api/channels/{name}/users/{user_id}/conversation
 *   POST   /api/channels/{name}/restart
 *   POST   /api/channels/{name}/approve
 *   POST   /api/channels/{name}/revoke
 *   POST   /api/channels/{name}/block
 *   POST   /api/channels/telegram/status
 *
 * The store keeps a thin in-memory cache so tab switches don't re-issue
 * GETs; the operator's Refresh button calls `fetchChannels` /
 * `fetchUsers` / `fetchConversation` to force a round-trip when a
 * snapshot gets stale (the Telegram poll cadence is ~25s on the bridge
 * side; user/allowlist mutations are operator-driven so a manual refresh
 * is enough — no polling loop). */
import { create } from 'zustand';
import { BACKEND_BASE } from '../lib/endpoints';
import type { Envelope } from '../lib/types';

const _LOG_RING_CAP = 200;

export type BridgeState = 'running' | 'stopped' | 'error';
export type TelegramOverride = 'online' | 'offline' | null;

export interface ChannelStatus {
  name: string;
  bridge_state: BridgeState;
  last_poll_at: string | null;
  error_count_24h: number;
  messages_in_24h: number;
  messages_out_24h: number;
  pending_count: number;
  allowed_count: number;
}

export interface ChannelRow {
  name: string;
  status_snapshot: ChannelStatus;
  extras: { override?: TelegramOverride | null };
}

interface MutationResult {
  approved: boolean;
  output: string;
}

export interface ChannelLogEntry {
  ts: string;
  level: string;
  logger: string;
  message: string;
}

export type ChannelUserTier = 'operator' | 'friend';
export type ChannelUserState = 'allowed' | 'pending' | 'blocked';

export interface ChannelUser {
  user_id: string;
  display_name: string;
  tier: ChannelUserTier;
  ttl_iso: string | null;
  first_seen: string;
  last_seen: string;
  messages_total: number;
  state: ChannelUserState;
}

export interface ConversationRow {
  ts: string;
  direction: 'inbound' | 'outbound';
  body: string;
  extra: Record<string, unknown>;
}

export interface ApproveInput {
  user_id: string;
  tier: ChannelUserTier;
  ttl_iso: string | null;
  display_name: string | null;
}

interface ChannelsState {
  channels: ChannelRow[];
  selectedChannel: string | null;
  loading: boolean;
  pending: Record<string, boolean>;
  error: string | null;
  // Per-channel ring buffer of `log_error` envelopes whose logger name
  // namespaces under `tesseract.integrations.<channel>.*`. Capped at
  // `_LOG_RING_CAP` newest-first so an error storm cannot grow the
  // store unbounded.
  logsByChannel: Record<string, ChannelLogEntry[]>;

  // MO-9-12 — Users + Conversations
  usersByChannel: Record<string, ChannelUser[]>;
  // Keyed by `${channel}:${user_id}` so two channels with overlapping IDs
  // (post-multi-channel milestone) cannot collide.
  conversationByUser: Record<string, ConversationRow[]>;
  selectedUserIdByChannel: Record<string, string | null>;

  fetchChannels: () => Promise<void>;
  selectChannel: (name: string) => void;
  restartChannel: (name: string, sessionId: string) => Promise<MutationResult>;
  setTelegramOverride: (
    override: TelegramOverride,
    sessionId: string,
  ) => Promise<MutationResult>;
  applyEnvelope: (env: Envelope) => void;
  clearLogs: (channel: string) => void;

  fetchUsers: (channel: string) => Promise<void>;
  fetchConversation: (channel: string, userId: string, limit?: number) => Promise<void>;
  selectUser: (channel: string, userId: string | null) => void;
  approveUser: (
    channel: string,
    input: ApproveInput,
    sessionId: string,
  ) => Promise<MutationResult>;
  revokeUser: (
    channel: string,
    userId: string,
    sessionId: string,
  ) => Promise<MutationResult>;
  blockUser: (
    channel: string,
    userId: string,
    sessionId: string,
  ) => Promise<MutationResult>;
}

export function conversationKey(channel: string, userId: string): string {
  return `${channel}:${userId}`;
}

async function _getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${BACKEND_BASE}${path}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

async function _postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BACKEND_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try {
      const err = (await res.json()) as { error?: string };
      if (err?.error) msg = err.error;
    } catch {
      // no JSON body — keep status code
    }
    throw new Error(msg);
  }
  return res.json() as Promise<T>;
}

export const useChannelsStore = create<ChannelsState>((set, get) => ({
  channels: [],
  selectedChannel: null,
  loading: false,
  pending: {},
  error: null,
  logsByChannel: {},
  usersByChannel: {},
  conversationByUser: {},
  selectedUserIdByChannel: {},

  fetchChannels: async () => {
    set({ loading: true, error: null });
    try {
      const data = await _getJson<{ channels: ChannelRow[] }>('/api/channels');
      const channels = data.channels ?? [];
      set((s) => ({
        channels,
        loading: false,
        // Preserve the operator's pick unless the channel disappeared
        // (would happen on a bridge crash + manual unregister). When
        // none is selected yet, auto-pick the first registered adapter.
        selectedChannel:
          s.selectedChannel && channels.some((c) => c.name === s.selectedChannel)
            ? s.selectedChannel
            : channels[0]?.name ?? null,
      }));
    } catch (err) {
      set({
        loading: false,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  },

  selectChannel: (name) => {
    set({ selectedChannel: name });
  },

  restartChannel: async (name, sessionId) => {
    set((s) => ({ pending: { ...s.pending, [name]: true }, error: null }));
    try {
      const result = await _postJson<{
        status: 'approved' | 'denied';
        output: string;
        channel?: ChannelRow;
      }>(`/api/channels/${encodeURIComponent(name)}/restart`, {
        session_id: sessionId,
      });
      set((s) => {
        const next = { ...s.pending };
        delete next[name];
        const channels =
          result.status === 'approved' && result.channel
            ? s.channels.map((c) => (c.name === name ? result.channel! : c))
            : s.channels;
        return { pending: next, channels };
      });
      return { approved: result.status === 'approved', output: result.output };
    } catch (err) {
      set((s) => {
        const next = { ...s.pending };
        delete next[name];
        return {
          pending: next,
          error: err instanceof Error ? err.message : String(err),
        };
      });
      throw err;
    }
  },

  setTelegramOverride: async (override, sessionId) => {
    const key = 'telegram:override';
    set((s) => ({ pending: { ...s.pending, [key]: true }, error: null }));
    try {
      const result = await _postJson<{
        status: 'approved' | 'denied';
        output: string;
        channel?: ChannelRow;
      }>('/api/channels/telegram/status', {
        session_id: sessionId,
        override,
      });
      set((s) => {
        const next = { ...s.pending };
        delete next[key];
        const channels =
          result.status === 'approved' && result.channel
            ? s.channels.map((c) =>
                c.name === 'telegram' ? result.channel! : c,
              )
            : s.channels;
        return { pending: next, channels };
      });
      return { approved: result.status === 'approved', output: result.output };
    } catch (err) {
      set((s) => {
        const next = { ...s.pending };
        delete next[key];
        return {
          pending: next,
          error: err instanceof Error ? err.message : String(err),
        };
      });
      throw err;
    }
  },

  applyEnvelope: (env) => {
    // `log_error` envelopes fan into the Channels tab when the logger
    // namespace identifies a channel bridge. Filtering happens here so
    // the LogsPane component just renders the bucket for its channel
    // without re-doing the routing on every render.
    if (env.type !== 'log_error') return;
    const data = env.data as {
      logger?: string;
      message?: string;
      level?: string;
      exc_type?: string;
      exc_message?: string;
    };
    const logger = typeof data.logger === 'string' ? data.logger : '';
    const match = /^tesseract\.integrations\.([a-z0-9_]+)/.exec(logger);
    if (!match) return;
    const channel = match[1];
    const exc = data.exc_type
      ? ` [${data.exc_type}${data.exc_message ? `: ${data.exc_message}` : ''}]`
      : '';
    const entry: ChannelLogEntry = {
      ts: env.timestamp ?? new Date().toISOString(),
      level: typeof data.level === 'string' ? data.level : 'ERROR',
      logger,
      message: `${data.message ?? ''}${exc}`,
    };
    set((s) => {
      const prior = s.logsByChannel[channel] ?? [];
      const next = [entry, ...prior].slice(0, _LOG_RING_CAP);
      return { logsByChannel: { ...s.logsByChannel, [channel]: next } };
    });
  },

  clearLogs: (channel) => {
    set((s) => {
      const next = { ...s.logsByChannel };
      delete next[channel];
      return { logsByChannel: next };
    });
  },

  fetchUsers: async (channel) => {
    const key = `${channel}:users`;
    set((s) => ({ pending: { ...s.pending, [key]: true }, error: null }));
    try {
      const data = await _getJson<{ users: ChannelUser[] }>(
        `/api/channels/${encodeURIComponent(channel)}/users`,
      );
      const users = data.users ?? [];
      set((s) => {
        const next = { ...s.pending };
        delete next[key];
        return {
          pending: next,
          usersByChannel: { ...s.usersByChannel, [channel]: users },
        };
      });
    } catch (err) {
      set((s) => {
        const next = { ...s.pending };
        delete next[key];
        return {
          pending: next,
          error: err instanceof Error ? err.message : String(err),
        };
      });
    }
  },

  fetchConversation: async (channel, userId, limit = 100) => {
    const ckey = conversationKey(channel, userId);
    const pendingKey = `${channel}:conv:${userId}`;
    // 2026-05-16: dedup in-flight requests so the 5s ConversationsPane
    // poll cannot pile up behind a slow backend. A racy ordering of two
    // resolves would otherwise let a stale snapshot overwrite a fresh
    // one. Reviewer P1-3.
    if (get().pending[pendingKey]) return;
    set((s) => ({ pending: { ...s.pending, [pendingKey]: true }, error: null }));
    try {
      const data = await _getJson<{ rows: ConversationRow[] }>(
        `/api/channels/${encodeURIComponent(channel)}/users/${encodeURIComponent(userId)}/conversation?limit=${limit}`,
      );
      set((s) => {
        const next = { ...s.pending };
        delete next[pendingKey];
        return {
          pending: next,
          conversationByUser: { ...s.conversationByUser, [ckey]: data.rows ?? [] },
        };
      });
    } catch (err) {
      set((s) => {
        const next = { ...s.pending };
        delete next[pendingKey];
        return {
          pending: next,
          error: err instanceof Error ? err.message : String(err),
        };
      });
    }
  },

  selectUser: (channel, userId) => {
    set((s) => ({
      selectedUserIdByChannel: { ...s.selectedUserIdByChannel, [channel]: userId },
    }));
  },

  approveUser: async (channel, input, sessionId) => {
    const key = `${channel}:approve:${input.user_id}`;
    set((s) => ({ pending: { ...s.pending, [key]: true }, error: null }));
    try {
      const result = await _postJson<{
        status: 'approved' | 'denied';
        output: string;
        user?: ChannelUser;
        person_record_error?: string | null;
      }>(`/api/channels/${encodeURIComponent(channel)}/approve`, {
        session_id: sessionId,
        user_id: input.user_id,
        tier: input.tier,
        ttl_iso: input.ttl_iso,
        display_name: input.display_name,
      });
      set((s) => {
        const next = { ...s.pending };
        delete next[key];
        return { pending: next };
      });
      // Re-pull the user list on approval so the row migrates from
      // "pending" to "allowed" without a manual Refresh. Skip on deny so
      // the operator sees the pending row still highlighted.
      if (result.status === 'approved') {
        await get().fetchUsers(channel);
      }
      return { approved: result.status === 'approved', output: result.output };
    } catch (err) {
      set((s) => {
        const next = { ...s.pending };
        delete next[key];
        return {
          pending: next,
          error: err instanceof Error ? err.message : String(err),
        };
      });
      throw err;
    }
  },

  revokeUser: async (channel, userId, sessionId) => {
    const key = `${channel}:revoke:${userId}`;
    set((s) => ({ pending: { ...s.pending, [key]: true }, error: null }));
    try {
      const result = await _postJson<{
        status: 'approved' | 'denied';
        output: string;
        user?: ChannelUser;
      }>(`/api/channels/${encodeURIComponent(channel)}/revoke`, {
        session_id: sessionId,
        user_id: userId,
      });
      set((s) => {
        const next = { ...s.pending };
        delete next[key];
        return { pending: next };
      });
      if (result.status === 'approved') {
        await get().fetchUsers(channel);
      }
      return { approved: result.status === 'approved', output: result.output };
    } catch (err) {
      set((s) => {
        const next = { ...s.pending };
        delete next[key];
        return {
          pending: next,
          error: err instanceof Error ? err.message : String(err),
        };
      });
      throw err;
    }
  },

  blockUser: async (channel, userId, sessionId) => {
    const key = `${channel}:block:${userId}`;
    set((s) => ({ pending: { ...s.pending, [key]: true }, error: null }));
    try {
      const result = await _postJson<{
        status: 'approved' | 'denied';
        output: string;
        user?: ChannelUser;
      }>(`/api/channels/${encodeURIComponent(channel)}/block`, {
        session_id: sessionId,
        user_id: userId,
      });
      set((s) => {
        const next = { ...s.pending };
        delete next[key];
        return { pending: next };
      });
      if (result.status === 'approved') {
        await get().fetchUsers(channel);
      }
      return { approved: result.status === 'approved', output: result.output };
    } catch (err) {
      set((s) => {
        const next = { ...s.pending };
        delete next[key];
        return {
          pending: next,
          error: err instanceof Error ? err.message : String(err),
        };
      });
      throw err;
    }
  },
}));

// Selector helpers — used by the StatusPane so its useStore subscription
// is keyed on a single channel name instead of the full channel array.
export function selectChannelByName(state: ChannelsState, name: string | null) {
  if (!name) return null;
  return state.channels.find((c) => c.name === name) ?? null;
}

export function selectIsPending(
  state: ChannelsState,
  key: string,
): boolean {
  return Boolean(state.pending[key]);
}

// Frozen empty arrays so a channel without an entry returns a stable
// reference — without this, Zustand's default Object.is comparator would
// see a fresh [] on every render and tear the React tree down with an
// infinite re-render loop.
const _EMPTY_USERS: readonly ChannelUser[] = Object.freeze([]);
const _EMPTY_CONV: readonly ConversationRow[] = Object.freeze([]);
const _EMPTY_LOGS: readonly ChannelLogEntry[] = Object.freeze([]);

export function selectLogsForChannel(
  state: ChannelsState,
  channel: string,
): readonly ChannelLogEntry[] {
  return state.logsByChannel[channel] ?? _EMPTY_LOGS;
}

export function selectUsersForChannel(
  state: ChannelsState,
  channel: string,
): readonly ChannelUser[] {
  return state.usersByChannel[channel] ?? _EMPTY_USERS;
}

export function selectConversation(
  state: ChannelsState,
  channel: string,
  userId: string | null,
): readonly ConversationRow[] {
  if (!userId) return _EMPTY_CONV;
  return (
    state.conversationByUser[conversationKey(channel, userId)] ?? _EMPTY_CONV
  );
}
