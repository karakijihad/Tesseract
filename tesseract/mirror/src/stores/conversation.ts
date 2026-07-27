import { create, type StoreApi } from 'zustand';
import type {
  ApprovalRequest,
  AssistantStreamSegment,
  ChatAttachment,
  ChatMessage,
  CliStreamState,
  ModelSelectedData,
  MessageStats,
  ToolCall,
  ToolCallStatus,
  ToolResult,
  ToolStatusEntry,
} from '../lib/types';
import { getTtsPlayer } from '../lib/voice/tts-player';
import { buildViewSnapshot } from '../lib/viewSnapshot';
import { useWebSocketStore } from './websocket';

// mirror-multi-chat inc.B — per-chat slice. Each open chat owns an isolated
// copy of the streaming/turn/message state that used to be a single flat set
// of store fields. `chat_id` (a 32-hex id seeded from `session_created`,
// stamped on turn-scoped envelopes by inc.A) keys the slice. Voice/TTS state
// (`dropTtsUntilTurnEnd`) is NOT here — it stays session-global on the store
// (D8: voice speaks to the active chat only). See `_shared/store-shapes.md`.
export interface ChatSlice {
  // P3 — operator-facing tab label. Date-stamp by default (D2), set from the
  // backend via chat_created / session_created hydration; renamable.
  title: string;
  messages: ChatMessage[];
  streamingMessageId: string | null;
  streamingText: string;
  streamingStatusText: string;
  streamingSegments: AssistantStreamSegment[];
  pendingApprovals: ApprovalRequest[];
  isStreaming: boolean;
  currentTurn: string | null;
  currentToolCalls: ToolCall[];
  currentToolResults: ToolResult[];
  toolStatus: Map<string, ToolStatusEntry>;
  cliStreams: Map<string, CliStreamState>;
  messageModel: Map<string, ModelSelectedData>;
  messageStats: Map<string, MessageStats>;
  backgroundCalls: Set<string>;
}

// Every mutating action takes a `chatId: string | null` first arg (decision 1
// — mandatory threading). `null` resolves to `activeChatId`, the documented
// fallback for untagged/legacy envelopes. Dispatch passes `env.chat_id`;
// components pass an explicit id or `null` for "the active chat".
interface ChatState {
  chats: Map<string, ChatSlice>;
  orderedIds: string[];
  activeChatId: string | null;
  dropTtsUntilTurnEnd: boolean;
  getActiveSlice: () => ChatSlice | null;
  getSlice: (chatId: string) => ChatSlice | null;
  // Ensure a slice exists for `chatId` and make it active. Idempotent — an
  // existing slice (e.g. across a transient reconnect with the same id) is
  // preserved, not clobbered. Called from dispatch on `session_created`.
  initChat: (chatId: string) => void;
  // P3 — set a chat's tab label (rename, or seed from backend title).
  setChatTitle: (chatId: string, title: string) => void;
  // P3 — drop a chat from the open set (soft-archive, D1). Removes its slice +
  // orderedIds entry; if it was active, switches to the newest remaining (or
  // null when none remain).
  archiveChat: (chatId: string) => void;
  // P3 — reseed the open-chat set from the backend on (re)connect so the tab
  // strip survives a page reload. `list` is in display order (newest-first);
  // an existing slice's messages are preserved, only the title is refreshed.
  hydrateChats: (list: { chatId: string; title: string }[], activeChatId: string | null) => void;
  markInterrupted: (chatId: string | null) => void;
  // inc.C2: interrupt EVERY streaming slice (not just the active one). The WS
  // disconnect handler calls this — with parallel background streaming, an
  // unexpected close strands in-flight turns across multiple chats.
  interruptAllStreaming: () => void;
  reset: (chatId: string | null) => void;
  loadHistory: (
    chatId: string | null,
    messages: ChatMessage[],
    seedMeta?: { modelById?: Map<string, ModelSelectedData>; statsById?: Map<string, MessageStats> },
  ) => void;
  sendUserMessage: (chatId: string | null, content: string, attachments?: ChatAttachment[]) => void;
  appendUserMessage: (chatId: string | null, content: string) => void;
  // Q3 frontend — "redirect now": WS `steer` (distinct from the FIFO queue
  // default). Renders a `steered` bubble immediately (optimistic, same
  // precedent as `sendUserMessage`'s `queued` bubble) rather than waiting
  // on the backend's `steered` confirmation envelope, which is a no-op on
  // the dispatch side (see dispatch.ts `_handleLoop`).
  sendSteer: (chatId: string | null, text: string) => void;
  cancelStream: (chatId: string | null) => void;
  stopVoice: () => void;
  clearTtsDropFlag: () => void;
  beginTurn: (chatId: string | null, turn: string) => void;
  appendDelta: (chatId: string | null, text: string, kind?: 'intent' | 'status' | 'answer') => void;
  addToolCall: (chatId: string | null, tc: ToolCall) => void;
  addToolResult: (chatId: string | null, tr: ToolResult) => void;
  completeTurn: (chatId: string | null, turn: string, stopReason?: string) => void;
  endStream: (chatId: string | null, reason: string) => void;
  addApproval: (chatId: string | null, a: ApprovalRequest) => void;
  clearApproval: (chatId: string | null, callId: string) => void;
  resolveApproval: (chatId: string | null, callId: string, approved: boolean) => void;
  addEntityMessage: (chatId: string | null, message: string) => void;
  addError: (chatId: string | null, message: string) => void;
  addStreamNote: (chatId: string | null, text: string) => void;
  setToolStatus: (chatId: string | null, callId: string, status: ToolCallStatus, reason?: string) => void;
  startCli: (chatId: string | null, callId: string, tool: string) => void;
  appendCliLine: (chatId: string | null, callId: string, delta: string) => void;
  endCli: (chatId: string | null, callId: string, exitCode: number) => void;
  setMessageModel: (chatId: string | null, info: ModelSelectedData) => void;
  setMessageStats: (chatId: string | null, stats: MessageStats) => void;
  // Phase 2 (CLI parity, revised 2026-05-11) — when the backend's
  // mid-turn drain fires `stream_user_inject`, flip the oldest N
  // queued user bubbles to `complete` (FIFO order matches the
  // backend drain order). Removes the previous standalone badge UI;
  // the user's own bubble is the canonical "queued" indicator.
  markQueuedDelivered: (chatId: string | null, count: number) => void;
  // Task 5.2 review fix-pass — the backend's `steered` envelope can arrive
  // with `applied: false` when a focused-chat steer degraded to a normal
  // `_start_turn` send (no active turn to redirect). `sendSteer` already
  // rendered the bubble optimistically as `steered: true`; this clears
  // that flag so the "redirected" pill doesn't linger on what was
  // actually a fresh normal turn. Correlates by chat_id + newest
  // `steered: true` bubble (no message-id round-trips in this envelope).
  clearDegradedSteer: (chatId: string | null) => void;
  // Phase 4 (CLI parity) — set of tool call_ids that were dispatched
  // with `background=true`. Used by DelegateCard to render a "↻
  // background" badge. Populated in dispatch.ts on stream_tool_call_end
  // when the input declares background:true.
  markCallBackground: (chatId: string | null, call_id: string) => void;
}

// Stable empty fallbacks so consumer selectors that read a slice field on a
// missing/pre-connect slice (`getActiveSlice()?.messages ?? EMPTY_MESSAGES`)
// return a referentially-stable value — a fresh `[]` each render would defeat
// Zustand's Object.is bail-out and re-render in a loop.
export const EMPTY_MESSAGES: ChatMessage[] = [];
export const EMPTY_APPROVALS: ApprovalRequest[] = [];
export const EMPTY_TOOL_CALLS: ToolCall[] = [];
export const EMPTY_TOOL_RESULTS: ToolResult[] = [];
export const EMPTY_SEGMENTS: AssistantStreamSegment[] = [];
export const EMPTY_TOOL_STATUS: Map<string, ToolStatusEntry> = new Map();
export const EMPTY_CLI_STREAMS: Map<string, CliStreamState> = new Map();
export const EMPTY_MESSAGE_MODEL: Map<string, ModelSelectedData> = new Map();
export const EMPTY_MESSAGE_STATS: Map<string, MessageStats> = new Map();
export const EMPTY_BACKGROUND_CALLS: Set<string> = new Set();

function _makeSlice(): ChatSlice {
  return {
    title: '',
    messages: [],
    streamingMessageId: null,
    streamingText: '',
    streamingStatusText: '',
    streamingSegments: [],
    pendingApprovals: [],
    isStreaming: false,
    currentTurn: null,
    currentToolCalls: [],
    currentToolResults: [],
    toolStatus: new Map(),
    cliStreams: new Map(),
    messageModel: new Map(),
    messageStats: new Map(),
    backgroundCalls: new Set(),
  };
}

const CLI_MAX_LINES = 500;

// rAF-gated streaming-text flush, now PER CHAT. Each `stream_text` envelope
// produces one delta; without batching, every delta triggers a Zustand
// `set()` → React render, so text arrives in network-cadence steps. Buffering
// deltas and flushing once per animation frame collapses N renders/frame into
// 1 (how Claude.ai / ChatGPT stream smoothly).
//
// Decision 3: the buffer + rAF handle live in a module-level `Map<chatId,
// RafScratch>`, NOT inside the immutable ChatSlice. Zustand replaces the slice
// object on every `set()`, which would strand a mutable handle on a stale
// object. A keyed registry gives the per-chat isolation the contract wants
// (two streaming chats can't corrupt each other's batch) without fighting
// immutability. `_flushPendingDelta` is called by completeTurn /
// markInterrupted / cancelStream so no chunks are lost across turn boundaries.
interface RafScratch {
  pending: AssistantStreamSegment[];
  handle: number | null;
}
const _rafByChat = new Map<string, RafScratch>();

// FIFO queue-position counter, PER CHAT (Q2 frontend). `sendUserMessage`
// assigns the next `queuePosition` optimistically, client-side, at send
// time — before any server ack. Lives in a module-level registry for the
// same reason `_rafByChat` does: Zustand replaces the slice object on every
// `set()`, so a mutable counter can't live inside the immutable ChatSlice.
// Reset to 0 whenever the queue drains to empty (a non-queued send, or a
// slice wipe) so the next queued run starts a fresh 1/2/3 sequence.
const _queueCounterByChat = new Map<string, number>();

function _nextQueuePosition(id: string): number {
  const next = (_queueCounterByChat.get(id) ?? 0) + 1;
  _queueCounterByChat.set(id, next);
  return next;
}

function _scratch(chatId: string): RafScratch {
  let sc = _rafByChat.get(chatId);
  if (!sc) {
    sc = { pending: [], handle: null };
    _rafByChat.set(chatId, sc);
  }
  return sc;
}

function _raf(cb: FrameRequestCallback): number {
  return typeof requestAnimationFrame === 'function'
    ? requestAnimationFrame(cb)
    : (setTimeout(() => cb(performance.now()), 16) as unknown as number);
}

// Text segments (intent/answer) merge with the previous segment of the
// same kind; tool_call segments are always discrete (no merging — each
// pill is its own timeline entry, keyed by call_id).
function _appendSegment(
  segments: AssistantStreamSegment[],
  kind: AssistantStreamSegment['kind'],
  text: string,
): AssistantStreamSegment[] {
  if (kind === 'tool_call') {
    // Caller should use _appendToolCallSegment for this; guard anyway.
    return segments;
  }
  if (!text) return segments;
  const last = segments[segments.length - 1];
  if (last?.kind === kind) {
    return [
      ...segments.slice(0, -1),
      { ...last, text: last.text + text },
    ];
  }
  return [...segments, { kind, text }];
}

// A tool_call segment is appended at the moment `tool_call_start` arrives
// from the backend, so its position in the array reflects when it fired
// relative to the surrounding text. The pill renders inline by looking
// up the live result via `call_id`.
function _appendToolCallSegment(
  segments: AssistantStreamSegment[],
  callId: string,
  name: string,
): AssistantStreamSegment[] {
  if (segments.some(s => s.kind === 'tool_call' && s.call_id === callId)) {
    return segments;
  }
  return [...segments, { kind: 'tool_call', text: '', call_id: callId, name }];
}

function _appendSegments(
  base: AssistantStreamSegment[],
  incoming: AssistantStreamSegment[],
): AssistantStreamSegment[] {
  return incoming.reduce(
    (acc, segment) => _appendSegment(acc, segment.kind, segment.text),
    base,
  );
}

function _segmentsText(
  segments: AssistantStreamSegment[],
  kind: 'intent' | 'answer',
): string {
  return segments
    .filter(segment => segment.kind === kind)
    .map(segment => segment.text)
    .join('');
}

type SetState = StoreApi<ChatState>['setState'];

// Resolve the chat a mutation targets. An explicit id that names a known chat
// wins. An explicit id we DON'T know — a stray turn-scoped envelope for a chat
// archived/closed mid-stream — is IGNORED (returns null): it must NOT fall
// through to the active chat and corrupt it. Only an untagged (null) target
// uses the active-chat fallback (the legacy/voice path). Returns null when
// nothing resolves (e.g. pre-connect) — callers no-op in that case.
function _resolveId(state: ChatState, chatId: string | null): string | null {
  if (chatId) return state.chats.has(chatId) ? chatId : null;
  const a = state.activeChatId;
  return a && state.chats.has(a) ? a : null;
}

// Immutably patch one slice. `id` must already be a resolved, existing chat
// id. The updater returns a partial slice, or `null` to skip the write
// entirely (returning `{}` would still copy the Map — return `null` to no-op).
function _patchSlice(
  set: SetState,
  id: string,
  fn: (slice: ChatSlice) => Partial<ChatSlice> | null,
): void {
  set(state => {
    const slice = state.chats.get(id);
    if (!slice) return {};
    const patch = fn(slice);
    if (!patch) return {};
    const chats = new Map(state.chats);
    chats.set(id, { ...slice, ...patch });
    return { chats };
  });
}

// Merge rAF-buffered segments into a slice's streaming state. Shared by every
// path that flushes pending deltas (delta arrival, tool_call boundary, turn
// completion, interruption).
function _flushMergeFn(set: SetState, id: string): (incoming: AssistantStreamSegment[]) => void {
  return (incoming) =>
    _patchSlice(set, id, slice => {
      const streamingSegments = _appendSegments(slice.streamingSegments, incoming);
      return {
        streamingSegments,
        streamingText: _segmentsText(streamingSegments, 'answer'),
        streamingStatusText: _segmentsText(streamingSegments, 'intent'),
      };
    });
}

function _queuePendingSegment(id: string, kind: AssistantStreamSegment['kind'], text: string): void {
  const sc = _scratch(id);
  sc.pending = _appendSegment(sc.pending, kind, text);
}

function _scheduleFlush(id: string, flush: (segments: AssistantStreamSegment[]) => void): void {
  const sc = _scratch(id);
  if (sc.handle !== null) return;
  sc.handle = _raf(() => {
    sc.handle = null;
    if (!sc.pending.length) return;
    const segments = sc.pending;
    sc.pending = [];
    flush(segments);
  });
}

function _flushPendingDelta(id: string, flush: (segments: AssistantStreamSegment[]) => void): void {
  const sc = _scratch(id);
  if (sc.handle !== null && typeof cancelAnimationFrame === 'function') {
    cancelAnimationFrame(sc.handle);
  }
  sc.handle = null;
  if (!sc.pending.length) return;
  const segments = sc.pending;
  sc.pending = [];
  flush(segments);
}

function _dropPendingDelta(id: string): void {
  const sc = _scratch(id);
  if (sc.handle !== null && typeof cancelAnimationFrame === 'function') {
    cancelAnimationFrame(sc.handle);
  }
  sc.handle = null;
  sc.pending = [];
}

function _freezeInterrupted(
  messages: ChatMessage[],
  id: string,
  text: string,
  statusText: string,
  segments: AssistantStreamSegment[],
  calls: ToolCall[],
  results: ToolResult[],
): Partial<ChatSlice> {
  return {
    messages: [
      ...messages,
      {
        id,
        role: 'assistant' as const,
        content: text || '[interrupted]',
        statusText: statusText || undefined,
        segments: segments.length > 0 ? segments : undefined,
        timestamp: Date.now(),
        toolCalls: calls.length > 0 ? calls : undefined,
        toolResults: results.length > 0 ? results : undefined,
        status: 'interrupted' as const,
      },
    ],
    streamingMessageId: null,
    streamingText: '',
    streamingStatusText: '',
    streamingSegments: [],
    isStreaming: false,
    currentTurn: null,
    currentToolCalls: [],
    currentToolResults: [],
  };
}

export const useConversationStore = create<ChatState>((set, get) => ({
  chats: new Map<string, ChatSlice>(),
  orderedIds: [] as string[],
  activeChatId: null as string | null,
  dropTtsUntilTurnEnd: false,

  getActiveSlice: () => {
    const s = get();
    return s.activeChatId ? (s.chats.get(s.activeChatId) ?? null) : null;
  },

  getSlice: (chatId: string) => get().chats.get(chatId) ?? null,

  initChat: (chatId: string) => {
    set(state => {
      if (state.chats.has(chatId)) {
        return state.activeChatId === chatId ? {} : { activeChatId: chatId };
      }
      const chats = new Map(state.chats);
      chats.set(chatId, _makeSlice());
      const orderedIds = state.orderedIds.includes(chatId)
        ? state.orderedIds
        : [chatId, ...state.orderedIds];
      return { chats, orderedIds, activeChatId: chatId };
    });
  },

  setChatTitle: (chatId, title) => {
    set(state => {
      const slice = state.chats.get(chatId);
      if (!slice) return {};
      const chats = new Map(state.chats);
      chats.set(chatId, { ...slice, title });
      return { chats };
    });
  },

  archiveChat: (chatId) => {
    _dropPendingDelta(chatId);
    _rafByChat.delete(chatId); // slice is dropped; drop its rAF scratch
    _queueCounterByChat.delete(chatId); // and its FIFO queue-position counter
    set(state => {
      if (!state.chats.has(chatId)) return {};
      const chats = new Map(state.chats);
      chats.delete(chatId);
      const orderedIds = state.orderedIds.filter(id => id !== chatId);
      const activeChatId =
        state.activeChatId === chatId ? (orderedIds[0] ?? null) : state.activeChatId;
      return { chats, orderedIds, activeChatId };
    });
  },

  hydrateChats: (list, activeChatId) => {
    set(state => {
      const chats = new Map(state.chats);
      for (const { chatId, title } of list) {
        const existing = chats.get(chatId);
        chats.set(chatId, existing ? { ...existing, title } : { ..._makeSlice(), title });
      }
      return { chats, orderedIds: list.map(c => c.chatId), activeChatId };
    });
  },

  markInterrupted: (chatId) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    _flushPendingDelta(id, _flushMergeFn(set, id));
    const slice = get().chats.get(id);
    if (!slice || slice.streamingMessageId === null) return;
    _patchSlice(set, id, s =>
      _freezeInterrupted(s.messages, s.streamingMessageId as string, s.streamingText, s.streamingStatusText, s.streamingSegments, s.currentToolCalls, s.currentToolResults),
    );
    set({ dropTtsUntilTurnEnd: false });
  },

  interruptAllStreaming: () => {
    // Snapshot the ids first — markInterrupted mutates the chats Map.
    for (const [id, slice] of [...get().chats.entries()]) {
      if (slice.isStreaming) get().markInterrupted(id);
    }
  },

  reset: (chatId) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    _dropPendingDelta(id);
    _rafByChat.delete(id); // the slice is replaced wholesale; drop its rAF scratch
    _queueCounterByChat.delete(id); // and its FIFO queue-position counter
    set(state => {
      const chats = new Map(state.chats);
      chats.set(id, _makeSlice());
      return { chats, dropTtsUntilTurnEnd: false };
    });
  },

  loadHistory: (chatId, messages, seedMeta) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    _dropPendingDelta(id);
    _rafByChat.delete(id); // slice replaced wholesale; drop its rAF scratch
    _queueCounterByChat.delete(id); // and its FIFO queue-position counter
    set(state => {
      const chats = new Map(state.chats);
      chats.set(id, {
        ..._makeSlice(),
        messages,
        messageModel: seedMeta?.modelById ? new Map(seedMeta.modelById) : new Map(),
        messageStats: seedMeta?.statsById ? new Map(seedMeta.statsById) : new Map(),
      });
      return { chats };
    });
  },

  sendUserMessage: (chatId, content: string, attachments: ChatAttachment[] = []) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    // If TARS is mid-turn, the backend now queues this message FIFO (per
    // chat) and drains the front of the queue when the active turn ends.
    // Mark the bubble as `queued` so the UI shows a pending pill, and
    // assign it the next FIFO slot from a per-chat counter — optimistic,
    // client-side, at send time (before any server ack). `beginTurn`
    // flips the FRONT queued bubble (lowest `queuePosition`) to `complete`
    // when its turn starts; `markQueuedDelivered` flips a mid-turn-drained
    // prefix the same way it always has.
    const queued = get().chats.get(id)?.isStreaming ?? false;
    let queuePosition: number | undefined;
    if (queued) {
      queuePosition = _nextQueuePosition(id);
    } else {
      _queueCounterByChat.set(id, 0);
    }
    _patchSlice(set, id, s => ({
      messages: [
        ...s.messages,
        {
          id: `user-${Date.now()}`,
          role: 'user' as const,
          content,
          attachments: attachments.length > 0 ? attachments : undefined,
          timestamp: Date.now(),
          status: queued ? ('queued' as const) : ('complete' as const),
          queuePosition,
        },
      ],
    }));
    useWebSocketStore.getState().sendMessage('chat_message', {
      text: content,
      attachments: attachments.map(a => ({ id: a.id })),
      view_snapshot: buildViewSnapshot(),
    });
  },

  /** Voice path — backend already dispatched the turn server-side; we
   * just need the user-message bubble to render in the chat history.
   * No WS send. ID uses `crypto.randomUUID()` so two voice_finals
   * arriving within the same millisecond don't collide on key. */
  appendUserMessage: (chatId, content: string) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    const msgId = typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
      ? `user-voice-${crypto.randomUUID()}`
      : `user-voice-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    _patchSlice(set, id, s => ({
      messages: [
        ...s.messages,
        {
          id: msgId,
          role: 'user' as const,
          content,
          timestamp: Date.now(),
          status: 'complete' as const,
        },
      ],
    }));
  },

  // Q3 frontend — "redirect now". Renders the operator's redirect
  // as a normal, immediately-"complete" user bubble (never queued — a
  // steer is not a FIFO follow-up) flagged `steered: true` for the pill,
  // then sends WS `steer` with the resolved `chat_id` so a background-
  // chat steer (future use) routes correctly server-side, not just the
  // implicit focused chat.
  sendSteer: (chatId, text: string) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    const trimmed = text.trim();
    if (!trimmed) return;
    _patchSlice(set, id, s => ({
      messages: [
        ...s.messages,
        {
          id: `user-${Date.now()}`,
          role: 'user' as const,
          content: trimmed,
          timestamp: Date.now(),
          status: 'complete' as const,
          steered: true,
        },
      ],
    }));
    useWebSocketStore.getState().sendMessage('steer', { chat_id: id, text: trimmed });
  },

  cancelStream: (chatId) => {
    const id = _resolveId(get(), chatId);
    if (id) _dropPendingDelta(id);
    getTtsPlayer().cancel();
    set({ dropTtsUntilTurnEnd: true });
    useWebSocketStore.getState().sendMessage('cancel_stream', {});
  },

  stopVoice: () => {
    getTtsPlayer().cancel();
    set({ dropTtsUntilTurnEnd: true });
    useWebSocketStore.getState().sendMessage('voice_cancel', {});
  },

  clearTtsDropFlag: () => set({ dropTtsUntilTurnEnd: false }),

  beginTurn: (chatId, turn: string) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    _dropPendingDelta(id);
    _patchSlice(set, id, state => {
      // FIFO (Q2 frontend): the backend now queues messages per chat in
      // send order and drains the FRONT of the queue when a turn starts —
      // it no longer silently overwrites earlier queued messages. Flip the
      // oldest queued bubble (lowest `queuePosition`) to `complete`; it's
      // the one whose turn is actually beginning. Everything else stays
      // `queued`, re-numbered 1..N (stable FIFO order) so the UI never
      // shows a gap after the front slot is removed.
      const queuedEntries = state.messages
        .map((m, i) => ({ m, i }))
        .filter(({ m }) => m.status === 'queued');
      let front: { m: (typeof queuedEntries)[number]['m']; i: number } | null = null;
      for (const entry of queuedEntries) {
        const entryPos = entry.m.queuePosition ?? Number.POSITIVE_INFINITY;
        const bestPos = front ? (front.m.queuePosition ?? Number.POSITIVE_INFINITY) : Number.POSITIVE_INFINITY;
        if (front === null || entryPos < bestPos) front = entry;
      }
      const frontIdx = front ? front.i : -1;
      const remaining = queuedEntries
        .filter(({ i }) => i !== frontIdx)
        .sort((a, b) => (a.m.queuePosition ?? Number.POSITIVE_INFINITY) - (b.m.queuePosition ?? Number.POSITIVE_INFINITY));
      const renumbered = new Map<number, number>();
      remaining.forEach(({ i }, order) => renumbered.set(i, order + 1));

      const messages = state.messages.map((m, i) => {
        if (m.status !== 'queued') return m;
        if (i === frontIdx) {
          return { ...m, status: 'complete' as const, queuePosition: undefined };
        }
        return { ...m, queuePosition: renumbered.get(i) };
      });

      // Continue the FIFO sequence from where the renumbered remainder
      // left off, so the next queued send during this new turn gets the
      // next slot instead of restarting at 1.
      _queueCounterByChat.set(id, remaining.length);

      return {
        messages,
        streamingMessageId: `turn-${turn}`,
        streamingText: '',
        streamingStatusText: '',
        streamingSegments: [],
        isStreaming: true,
        currentTurn: turn,
        currentToolCalls: [],
        currentToolResults: [],
      };
    });
    set({ dropTtsUntilTurnEnd: false });
  },

  appendDelta: (chatId, text: string, kind = 'answer') => {
    if (!text) return;
    const id = _resolveId(get(), chatId);
    if (!id) return;
    const segmentKind = kind === 'status' || kind === 'intent' ? 'intent' : 'answer';
    _queuePendingSegment(id, segmentKind, text);
    _scheduleFlush(id, _flushMergeFn(set, id));
  },

  addToolCall: (chatId, tc: ToolCall) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    // Flush any pending text deltas first so the tool_call segment lands
    // AFTER the intent/answer text that streamed before it. Without this,
    // a delta queued on the rAF could land *after* the tool_call segment
    // in the array even though it was emitted first on the wire — the
    // operator would see "tool pill, then the intent that preceded it".
    _flushPendingDelta(id, _flushMergeFn(set, id));
    _patchSlice(set, id, s => ({
      currentToolCalls: [...s.currentToolCalls, tc],
      streamingSegments: _appendToolCallSegment(s.streamingSegments, tc.call_id, tc.name),
    }));
  },

  addToolResult: (chatId, tr: ToolResult) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    _patchSlice(set, id, s => ({ currentToolResults: [...s.currentToolResults, tr] }));
  },

  completeTurn: (chatId, _turn: string, _stopReason?: string) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    _flushPendingDelta(id, _flushMergeFn(set, id));
    const slice = get().chats.get(id);
    if (!slice || slice.streamingMessageId === null) return;
    _patchSlice(set, id, s => ({
      messages: [
        ...s.messages,
        {
          id: s.streamingMessageId as string,
          role: 'assistant' as const,
          content: s.streamingText,
          statusText: s.streamingStatusText || undefined,
          segments: s.streamingSegments.length > 0 ? s.streamingSegments : undefined,
          timestamp: Date.now(),
          toolCalls: s.currentToolCalls.length > 0 ? s.currentToolCalls : undefined,
          toolResults: s.currentToolResults.length > 0 ? s.currentToolResults : undefined,
          status: 'complete' as const,
        },
      ],
      streamingMessageId: null,
      streamingText: '',
      streamingStatusText: '',
      streamingSegments: [],
      isStreaming: false,
      currentTurn: null,
      currentToolCalls: [],
      currentToolResults: [],
    }));
    set({ dropTtsUntilTurnEnd: false });
  },

  endStream: (chatId, _reason: string) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    const slice = get().chats.get(id);
    if (!slice) return;
    if (slice.streamingMessageId !== null) {
      // Drain the rAF buffer first so a delta queued just before the stream
      // ended lands in the frozen bubble rather than being dropped (mirrors
      // markInterrupted).
      _flushPendingDelta(id, _flushMergeFn(set, id));
      _patchSlice(set, id, s =>
        _freezeInterrupted(s.messages, s.streamingMessageId as string, s.streamingText, s.streamingStatusText, s.streamingSegments, s.currentToolCalls, s.currentToolResults),
      );
    } else {
      _patchSlice(set, id, () => ({ isStreaming: false }));
    }
    set({ dropTtsUntilTurnEnd: false });
  },

  addApproval: (chatId, a: ApprovalRequest) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    _patchSlice(set, id, s => ({ pendingApprovals: [...s.pendingApprovals, a] }));
  },

  clearApproval: (chatId, callId: string) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    _patchSlice(set, id, s => ({
      pendingApprovals: s.pendingApprovals.filter(a => a.call_id !== callId),
    }));
  },

  resolveApproval: (chatId, callId: string, approved: boolean) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    // Idempotency guard — when ApprovalCard is mounted on multiple
    // surfaces (chat tab + HUD panel) the operator can click Approve in
    // either; without this, both instances fire `tool_response` for the
    // same call_id. Optimistically clear the entry first; if it's
    // already gone the second click is a no-op.
    const slice = get().chats.get(id);
    if (!slice || !slice.pendingApprovals.some(a => a.call_id === callId)) return;
    _patchSlice(set, id, s => ({
      pendingApprovals: s.pendingApprovals.filter(a => a.call_id !== callId),
    }));
    useWebSocketStore.getState().sendMessage('tool_response', { call_id: callId, approved });
  },

  addEntityMessage: (chatId, message: string) => {
    if (!message) return;
    const id = _resolveId(get(), chatId);
    if (!id) return;
    _patchSlice(set, id, s => ({
      messages: [
        ...s.messages,
        {
          id: `entity-${Date.now()}`,
          role: 'entity' as const,
          content: message,
          timestamp: Date.now(),
          status: 'complete' as const,
        },
      ],
    }));
  },

  addError: (chatId, message: string) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    // If a bubble is mid-stream, drain the rAF buffer first so a delta queued
    // just before the error lands in the frozen bubble rather than vanishing.
    if (get().chats.get(id)?.streamingMessageId != null) {
      _flushPendingDelta(id, _flushMergeFn(set, id));
    }
    _patchSlice(set, id, s => {
      const base = s.streamingMessageId !== null
        ? _freezeInterrupted(s.messages, s.streamingMessageId, s.streamingText, s.streamingStatusText, s.streamingSegments, s.currentToolCalls, s.currentToolResults)
        : { messages: s.messages, isStreaming: false };
      return {
        ...base,
        messages: [
          ...(base.messages ?? s.messages),
          {
            id: `error-${Date.now()}`,
            role: 'error' as const,
            content: message || 'error',
            timestamp: Date.now(),
            status: 'complete' as const,
          },
        ],
      };
    });
  },

  addStreamNote: (chatId, text: string) => {
    if (!text) return;
    const id = _resolveId(get(), chatId);
    if (!id) return;
    const slice = get().chats.get(id);
    if (!slice || slice.streamingMessageId === null) {
      // Soft errors are emitted by FallbackAdapter mid-turn — there
      // should always be an active streaming bubble when one arrives.
      // If not, the dispatch lifecycle is in an unexpected state; log
      // for diagnostics and drop the note rather than render a half-
      // formed standalone bubble (which would carry assistant chrome
      // — speaker, copy, regenerate — without the recovery context).
      console.warn('[chat] addStreamNote dropped — no active stream:', text);
      return;
    }
    // Active stream — flush any pending text deltas first so the note
    // lands AFTER the intent/answer text that streamed before it
    // (mirrors the addToolCall ordering rule), then append a discrete
    // system_note segment to the in-flight bubble. The bubble is NOT
    // frozen; the recovery iteration's deltas keep flowing into the
    // same message.
    _flushPendingDelta(id, _flushMergeFn(set, id));
    _patchSlice(set, id, s => ({
      streamingSegments: [
        ...s.streamingSegments,
        { kind: 'system_note' as const, text },
      ],
    }));
  },

  setToolStatus: (chatId, callId: string, status: ToolCallStatus, reason?: string) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    _patchSlice(set, id, s => {
      const next = new Map(s.toolStatus);
      next.set(callId, reason !== undefined ? { status, reason } : { status });
      return { toolStatus: next };
    });
  },

  startCli: (chatId, callId: string, tool: string) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    _patchSlice(set, id, s => {
      const next = new Map(s.cliStreams);
      next.set(callId, { tool, lines: [], started_at: Date.now() });
      return { cliStreams: next };
    });
  },

  appendCliLine: (chatId, callId: string, delta: string) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    _patchSlice(set, id, s => {
      const existing = s.cliStreams.get(callId);
      if (!existing) return null;
      const lines = existing.lines.concat(delta);
      const trimmed = lines.length > CLI_MAX_LINES ? lines.slice(-CLI_MAX_LINES) : lines;
      const next = new Map(s.cliStreams);
      next.set(callId, { ...existing, lines: trimmed });
      return { cliStreams: next };
    });
  },

  endCli: (chatId, callId: string, exitCode: number) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    _patchSlice(set, id, s => {
      const existing = s.cliStreams.get(callId);
      if (!existing) return null;
      const next = new Map(s.cliStreams);
      next.set(callId, { ...existing, exit_code: exitCode });
      return { cliStreams: next };
    });
  },

  setMessageModel: (chatId, info: ModelSelectedData) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    _patchSlice(set, id, s => {
      if (!s.streamingMessageId) return null;
      const next = new Map(s.messageModel);
      next.set(s.streamingMessageId, info);
      return { messageModel: next };
    });
  },

  setMessageStats: (chatId, stats: MessageStats) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    _patchSlice(set, id, s => {
      if (!s.streamingMessageId) return null;
      const prev = s.messageStats.get(s.streamingMessageId);
      // Accumulate across tool-loop iterations: sum input/output, latest cached value.
      const next = new Map(s.messageStats);
      next.set(s.streamingMessageId, {
        input_tokens: (prev?.input_tokens ?? 0) + stats.input_tokens,
        output_tokens: (prev?.output_tokens ?? 0) + stats.output_tokens,
        cached_tokens: (prev?.cached_tokens ?? 0) + stats.cached_tokens,
      });
      return { messageStats: next };
    });
  },

  markQueuedDelivered: (chatId, count: number) => {
    if (!Number.isFinite(count) || count <= 0) return;
    const id = _resolveId(get(), chatId);
    if (!id) return;
    _patchSlice(set, id, s => {
      let toFlip = count;
      const next: ChatMessage[] = [];
      for (const m of s.messages) {
        if (toFlip > 0 && m.role === 'user' && m.status === 'queued') {
          next.push({ ...m, status: 'complete' });
          toFlip -= 1;
        } else {
          next.push(m);
        }
      }
      return { messages: next };
    });
  },

  // Task 5.2 review fix-pass — finds the newest `steered: true` user
  // bubble in the chat and clears the flag. Correlation is by chat_id +
  // most-recent steered bubble (no message-id is round-tripped on the
  // `steered` envelope) — acceptable since a steer always targets the
  // latest thing the operator just sent.
  clearDegradedSteer: (chatId) => {
    const id = _resolveId(get(), chatId);
    if (!id) return;
    _patchSlice(set, id, s => {
      const idx = [...s.messages]
        .reverse()
        .findIndex(m => m.role === 'user' && m.steered);
      if (idx === -1) return null;
      const i = s.messages.length - 1 - idx;
      const next = [...s.messages];
      next[i] = { ...next[i], steered: false };
      return { messages: next };
    });
  },

  markCallBackground: (chatId, call_id: string) => {
    if (!call_id) return;
    const id = _resolveId(get(), chatId);
    if (!id) return;
    _patchSlice(set, id, s => {
      if (s.backgroundCalls.has(call_id)) return null;
      const next = new Set(s.backgroundCalls);
      next.add(call_id);
      return { backgroundCalls: next };
    });
  },
}));
