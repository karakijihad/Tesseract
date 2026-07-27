import { describe, it, expect, beforeEach, vi } from "vitest";

// dispatch.ts transitively opens a socket via the websocket store; stub it.
vi.mock("./websocket", () => ({
  useWebSocketStore: {
    getState: () => ({ sendMessage: vi.fn(), setSessionId: vi.fn() }),
  },
}));

import { handleEnvelope } from "./dispatch";
import { useConversationStore } from "./conversation";
import { useToastStore } from "./toasts";
import type { Envelope } from "../lib/types";

const CHAT_ID = "b".repeat(32);

const A = "a".repeat(32);

function resetStore() {
  useConversationStore.setState({
    chats: new Map(),
    orderedIds: [],
    activeChatId: null,
    dropTtsUntilTurnEnd: false,
  });
}

function loopEnv(type: string, data: Record<string, unknown>): Envelope {
  return {
    type,
    category: "loop",
    session_id: "s",
    chat_id: A,
    timestamp: "2026-07-11T00:00:00Z",
    data,
  } as Envelope;
}

function dispatch(type: string, data: Record<string, unknown>): void {
  handleEnvelope(loopEnv(type, data), { fromCatchup: true });
}

describe("dispatch — Q3 steer envelopes", () => {
  beforeEach(resetStore);

  it("'steered' is a no-op — the operator's bubble was already rendered by sendSteer", () => {
    const push = vi.spyOn(useToastStore.getState(), "push");
    expect(() => dispatch("steered", { text: "redirect this" })).not.toThrow();
    expect(push).not.toHaveBeenCalled();
    push.mockRestore();
  });

  it("'steered' with applied:false (focused-chat degrade, Task 5.2 review fix-pass) clears the optimistic steered flag", () => {
    const st = useConversationStore.getState();
    st.initChat(CHAT_ID);
    st.beginTurn(CHAT_ID, "1");
    st.sendSteer(CHAT_ID, "redirect this");

    const before = st
      .getSlice(CHAT_ID)!
      .messages.filter((m) => m.role === "user");
    expect(before[0].steered).toBe(true);

    handleEnvelope(
      {
        type: "steered",
        category: "loop",
        session_id: "s",
        chat_id: CHAT_ID,
        timestamp: "2026-07-11T00:00:00Z",
        data: { text: "redirect this", applied: false },
      } as Envelope,
      { fromCatchup: true },
    );

    const after = useConversationStore
      .getState()
      .getSlice(CHAT_ID)!
      .messages.filter((m) => m.role === "user");
    expect(after[0].steered).toBe(false);
  });

  it("'steer_rejected' toasts a warning naming the reason, no chat bubble", () => {
    const push = vi.spyOn(useToastStore.getState(), "push");
    dispatch("steer_rejected", {
      text: "redirect this",
      reason: "no active turn for background chat",
    });
    expect(push).toHaveBeenCalledWith(
      "Steer not applied — no active turn for background chat",
      "warning",
    );
    push.mockRestore();
  });

  it("'chat_queue_overflow' toasts a warning naming the queue size", () => {
    const push = vi.spyOn(useToastStore.getState(), "push");
    dispatch("chat_queue_overflow", { text: "dropped", queue_size: 5 });
    expect(push).toHaveBeenCalledWith(
      "Message dropped — queue full (5 pending)",
      "warning",
    );
    push.mockRestore();
  });
});
