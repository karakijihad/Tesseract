import { describe, it, expect, beforeEach, vi } from 'vitest';

// Same stub as conversation.test.ts — sendUserMessage fires a real WS send;
// stub it out so tests don't need a live socket.
vi.mock('./websocket', () => ({
  useWebSocketStore: {
    getState: () => ({ sendMessage: vi.fn(), setSessionId: vi.fn() }),
  },
}));

import { useConversationStore } from './conversation';

const A = 'a'.repeat(32);

function resetStore() {
  useConversationStore.setState({
    chats: new Map(),
    orderedIds: [],
    activeChatId: null,
    dropTtsUntilTurnEnd: false,
  });
}

describe('conversation store — FIFO queued bubbles + position (Q2 frontend)', () => {
  beforeEach(resetStore);

  it('3 sends during a streaming turn produce 3 queued bubbles with positions 1/2/3', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.beginTurn(A, '1'); // turn is now streaming

    st.sendUserMessage(A, 'first');
    st.sendUserMessage(A, 'second');
    st.sendUserMessage(A, 'third');

    const queued = useConversationStore
      .getState()
      .getSlice(A)!
      .messages.filter((m) => m.role === 'user');
    expect(queued).toHaveLength(3);
    expect(queued.map((m) => m.status)).toEqual(['queued', 'queued', 'queued']);
    expect(queued.map((m) => m.queuePosition)).toEqual([1, 2, 3]);
  });

  it('beginTurn flips the FRONT queued bubble (lowest position) to complete, FIFO — not last-wins', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.beginTurn(A, '1');
    st.sendUserMessage(A, 'first');
    st.sendUserMessage(A, 'second');
    st.sendUserMessage(A, 'third');
    st.completeTurn(A, '1');

    // Backend drains the front of the FIFO queue; a new turn begins for it.
    st.beginTurn(A, '2');

    const users = useConversationStore
      .getState()
      .getSlice(A)!
      .messages.filter((m) => m.role === 'user');
    expect(users.map((m) => m.content)).toEqual(['first', 'second', 'third']);
    // FRONT (oldest / position 1) flips live — not last-wins-collapse.
    expect(users[0].status).toBe('complete');
    expect(users[0].queuePosition).toBeUndefined();
    // The rest stay queued, never `interrupted`.
    expect(users[1].status).toBe('queued');
    expect(users[2].status).toBe('queued');
  });

  it('re-derives positions of remaining queued bubbles after the front flips (no gap)', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.beginTurn(A, '1');
    st.sendUserMessage(A, 'first');
    st.sendUserMessage(A, 'second');
    st.sendUserMessage(A, 'third');
    st.completeTurn(A, '1');
    st.beginTurn(A, '2');

    const users = useConversationStore
      .getState()
      .getSlice(A)!
      .messages.filter((m) => m.role === 'user');
    // 'second' and 'third' were positions 2/3 — renumbered to 1/2.
    expect(users[1].queuePosition).toBe(1);
    expect(users[2].queuePosition).toBe(2);
  });

  it('a subsequent send during the new turn continues the FIFO sequence (position 3, not 1)', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.beginTurn(A, '1');
    st.sendUserMessage(A, 'first');
    st.sendUserMessage(A, 'second');
    st.sendUserMessage(A, 'third');
    st.completeTurn(A, '1');
    st.beginTurn(A, '2'); // front ('first') flips live; 'second'/'third' -> 1/2

    st.sendUserMessage(A, 'fourth');

    const users = useConversationStore
      .getState()
      .getSlice(A)!
      .messages.filter((m) => m.role === 'user');
    expect(users.map((m) => m.content)).toEqual([
      'first',
      'second',
      'third',
      'fourth',
    ]);
    expect(users.at(-1)!.queuePosition).toBe(3);
  });

  it('a non-queued send resets the FIFO counter to a fresh 1/2/3 sequence', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.beginTurn(A, '1');
    st.sendUserMessage(A, 'first'); // position 1
    st.completeTurn(A, '1'); // drains — but 'first' bubble stays queued until beginTurn flips it
    st.beginTurn(A, '2'); // flips 'first' live, queue now empty, counter reset to 0
    st.completeTurn(A, '2'); // turn 2 done — not streaming now

    // Not streaming right now — this send goes live immediately (no queue).
    st.sendUserMessage(A, 'immediate');
    const immediate = useConversationStore
      .getState()
      .getSlice(A)!
      .messages.filter((m) => m.role === 'user')
      .at(-1)!;
    expect(immediate.status).toBe('complete');
    expect(immediate.queuePosition).toBeUndefined();

    st.beginTurn(A, '3');
    st.sendUserMessage(A, 'freshly-queued');

    const last = useConversationStore
      .getState()
      .getSlice(A)!
      .messages.filter((m) => m.role === 'user')
      .at(-1)!;
    expect(last.content).toBe('freshly-queued');
    expect(last.queuePosition).toBe(1);
  });

  it('markQueuedDelivered flips the oldest N queued bubbles to complete (regression, unchanged)', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.beginTurn(A, '1');
    st.sendUserMessage(A, 'first');
    st.sendUserMessage(A, 'second');
    st.sendUserMessage(A, 'third');

    st.markQueuedDelivered(A, 2);

    const users = useConversationStore
      .getState()
      .getSlice(A)!
      .messages.filter((m) => m.role === 'user');
    expect(users.map((m) => m.status)).toEqual([
      'complete',
      'complete',
      'queued',
    ]);
  });
});
