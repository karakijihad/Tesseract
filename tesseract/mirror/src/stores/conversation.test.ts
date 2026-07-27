import { describe, it, expect, beforeEach, vi } from 'vitest';

// The store imports the websocket store transitively; stub its send so the
// happy-path sendUserMessage test doesn't open a real socket.
vi.mock('./websocket', () => ({
  useWebSocketStore: {
    getState: () => ({ sendMessage: vi.fn(), setSessionId: vi.fn() }),
  },
}));

import { useConversationStore } from './conversation';

const A = 'a'.repeat(32);
const B = 'b'.repeat(32);

function resetStore() {
  useConversationStore.setState({
    chats: new Map(),
    orderedIds: [],
    activeChatId: null,
    dropTtsUntilTurnEnd: false,
  });
}

// Drive one full turn through the store for `chatId` and return the committed
// assistant message content. `completeTurn` flushes the rAF buffer
// synchronously, so no frame wait is needed.
function runTurn(chatId: string | null, text: string, turn: string): void {
  const st = useConversationStore.getState();
  st.beginTurn(chatId, turn);
  st.appendDelta(chatId, text, 'answer');
  st.completeTurn(chatId, turn);
}

describe('conversation store — multi-chat slice routing (inc.B)', () => {
  beforeEach(resetStore);

  it('initChat creates an isolated slice and makes it active', () => {
    useConversationStore.getState().initChat(A);
    const s = useConversationStore.getState();
    expect(s.activeChatId).toBe(A);
    expect(s.orderedIds).toEqual([A]);
    expect(s.getActiveSlice()?.messages).toEqual([]);
    expect(s.getSlice(A)).not.toBeNull();
  });

  it('initChat is idempotent and never clobbers an existing slice', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    runTurn(A, 'kept across reconnect', '1');
    st.initChat(A); // same id again (reconnect)
    const slice = useConversationStore.getState().getSlice(A)!;
    expect(slice.messages.at(-1)?.content).toBe('kept across reconnect');
    expect(useConversationStore.getState().orderedIds).toEqual([A]);
  });

  it('routes a turn to the addressed chat and commits an assistant message', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    runTurn(A, 'hello', '1');
    const slice = useConversationStore.getState().getSlice(A)!;
    expect(slice.isStreaming).toBe(false);
    expect(slice.streamingMessageId).toBeNull();
    expect(slice.messages.at(-1)?.role).toBe('assistant');
    expect(slice.messages.at(-1)?.content).toBe('hello');
  });

  it('interruptAllStreaming freezes every streaming slice (inc.C2 disconnect sweep)', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.initChat(B);

    // Two chats mid-stream; a third (active) chat is idle.
    st.beginTurn(A, '1');
    st.appendDelta(A, 'A partial', 'answer');
    st.beginTurn(B, '2');
    st.appendDelta(B, 'B partial', 'answer');
    expect(useConversationStore.getState().getSlice(A)!.isStreaming).toBe(true);
    expect(useConversationStore.getState().getSlice(B)!.isStreaming).toBe(true);

    useConversationStore.getState().interruptAllStreaming();

    // BOTH streaming slices are frozen, not just the active one.
    const a = useConversationStore.getState().getSlice(A)!;
    const b = useConversationStore.getState().getSlice(B)!;
    expect(a.isStreaming).toBe(false);
    expect(a.streamingMessageId).toBeNull();
    expect(b.isStreaming).toBe(false);
    expect(b.streamingMessageId).toBeNull();
  });

  it('isolates concurrent streaming + rAF buffers between two chats', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.initChat(B); // B is now active

    // Interleave two live turns; each chat owns its own pending-segment buffer.
    st.beginTurn(A, '1');
    st.beginTurn(B, '2');
    st.appendDelta(A, 'from-A', 'answer');
    st.appendDelta(B, 'from-B', 'answer');
    st.completeTurn(A, '1');
    st.completeTurn(B, '2');

    const a = useConversationStore.getState().getSlice(A)!;
    const b = useConversationStore.getState().getSlice(B)!;
    expect(a.messages.at(-1)?.content).toBe('from-A');
    expect(b.messages.at(-1)?.content).toBe('from-B');
    // No cross-contamination: each chat has exactly its own assistant bubble.
    expect(a.messages).toHaveLength(1);
    expect(b.messages).toHaveLength(1);
  });

  it('null chatId falls back to the active chat', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.initChat(B); // active = B
    runTurn(null, 'active-only', '9');
    expect(useConversationStore.getState().getSlice(B)!.messages.at(-1)?.content).toBe('active-only');
    expect(useConversationStore.getState().getSlice(A)!.messages).toEqual([]);
  });

  it('ignores an action addressed to an unknown chat_id (no active-chat bleed)', () => {
    const st = useConversationStore.getState();
    st.initChat(A); // active = A
    // A stray turn-scoped envelope tagged for a chat that was archived/closed
    // mid-stream must be ignored — NOT fall through to the active chat (which
    // would corrupt A). Only an untagged (null) target uses the active fallback.
    runTurn(B, 'stray-from-closed-chat', '7');
    expect(useConversationStore.getState().getSlice(A)!.messages).toEqual([]);
    expect(useConversationStore.getState().getSlice(B)).toBeNull();
  });

  it('reset clears only the addressed chat (D7), not its siblings', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.initChat(B);
    runTurn(A, 'keep-me', '1');
    runTurn(B, 'wipe-me', '2');
    st.reset(B);
    expect(useConversationStore.getState().getSlice(A)!.messages.at(-1)?.content).toBe('keep-me');
    expect(useConversationStore.getState().getSlice(B)!.messages).toEqual([]);
  });

  it('endStream flushes pending rAF deltas into the frozen bubble', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.beginTurn(A, '1');
    st.appendDelta(A, 'half-written', 'answer'); // queued on rAF, not yet flushed
    st.endStream(A, 'disconnect');
    const slice = useConversationStore.getState().getSlice(A)!;
    expect(slice.streamingMessageId).toBeNull();
    expect(slice.messages.at(-1)?.content).toBe('half-written');
    expect(slice.messages.at(-1)?.status).toBe('interrupted');
  });

  it('addError flushes pending rAF deltas before freezing, then appends the error', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.beginTurn(A, '1');
    st.appendDelta(A, 'partial', 'answer'); // queued on rAF, not yet flushed
    st.addError(A, 'boom');
    const slice = useConversationStore.getState().getSlice(A)!;
    const frozen = slice.messages.find((m) => m.role === 'assistant');
    expect(frozen?.content).toBe('partial');
    expect(frozen?.status).toBe('interrupted');
    expect(slice.messages.at(-1)?.role).toBe('error');
    expect(slice.messages.at(-1)?.content).toBe('boom');
  });

  it('reset deletes the per-chat rAF scratch (no unbounded growth)', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.beginTurn(A, '1');
    st.appendDelta(A, 'x', 'answer'); // creates a _rafByChat entry
    st.reset(A);
    // After reset the slice is empty and a fresh turn still works cleanly.
    runTurn(A, 'after-reset', '2');
    expect(useConversationStore.getState().getSlice(A)!.messages.at(-1)?.content).toBe('after-reset');
  });

  it('no-ops mutations when no chat is active (pre-connect)', () => {
    const st = useConversationStore.getState();
    expect(() => st.sendUserMessage(null, 'orphan')).not.toThrow();
    expect(() => st.appendDelta(null, 'orphan', 'answer')).not.toThrow();
    expect(useConversationStore.getState().chats.size).toBe(0);
  });

  it('keeps dropTtsUntilTurnEnd as a session-global flag (not per-slice)', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.cancelStream(A);
    expect(useConversationStore.getState().dropTtsUntilTurnEnd).toBe(true);
    st.clearTtsDropFlag();
    expect(useConversationStore.getState().dropTtsUntilTurnEnd).toBe(false);
    // The flag lives on the store root, never inside the slice.
    expect('dropTtsUntilTurnEnd' in (useConversationStore.getState().getSlice(A) as object)).toBe(false);
  });
});

describe('conversation store — P3 tab lifecycle (inc.1)', () => {
  beforeEach(resetStore);

  it('setChatTitle sets the slice title', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.setChatTitle(A, 'Vault notes');
    expect(useConversationStore.getState().getSlice(A)?.title).toBe('Vault notes');
  });

  it('setChatTitle no-ops for an unknown chat', () => {
    expect(() => useConversationStore.getState().setChatTitle(A, 'x')).not.toThrow();
    expect(useConversationStore.getState().getSlice(A)).toBeNull();
  });

  it('archiveChat removes a non-active chat, leaving active untouched', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.initChat(B); // orderedIds [B, A], active B
    st.archiveChat(A);
    const s = useConversationStore.getState();
    expect(s.getSlice(A)).toBeNull();
    expect(s.orderedIds).toEqual([B]);
    expect(s.activeChatId).toBe(B);
  });

  it('archiving the active chat switches to the newest remaining', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.initChat(B); // orderedIds [B, A], active B
    st.archiveChat(B);
    const s = useConversationStore.getState();
    expect(s.activeChatId).toBe(A);
    expect(s.orderedIds).toEqual([A]);
  });

  it('archiving the only chat clears activeChatId', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.archiveChat(A);
    const s = useConversationStore.getState();
    expect(s.activeChatId).toBeNull();
    expect(s.orderedIds).toEqual([]);
  });

  it('hydrateChats seeds titled slices, order, and active (reload)', () => {
    useConversationStore.getState().hydrateChats(
      [{ chatId: B, title: 'Newest' }, { chatId: A, title: 'Older' }],
      B,
    );
    const s = useConversationStore.getState();
    expect(s.orderedIds).toEqual([B, A]);
    expect(s.activeChatId).toBe(B);
    expect(s.getSlice(A)?.title).toBe('Older');
    expect(s.getSlice(B)?.title).toBe('Newest');
  });

  it('hydrateChats preserves an existing slice’s messages', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    runTurn(A, 'kept', '1');
    st.hydrateChats([{ chatId: A, title: 'T' }], A);
    const slice = useConversationStore.getState().getSlice(A)!;
    expect(slice.title).toBe('T');
    expect(slice.messages.at(-1)?.content).toBe('kept');
  });
});
