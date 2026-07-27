import { describe, it, expect, beforeEach, vi } from "vitest";

// Same stub as conversation.queue.test.ts — sendSteer fires a real WS send;
// stub it out so tests don't need a live socket.
const sendMessage = vi.fn();
vi.mock("./websocket", () => ({
  useWebSocketStore: {
    getState: () => ({ sendMessage, setSessionId: vi.fn() }),
  },
}));

import { useConversationStore } from "./conversation";

const A = "a".repeat(32);

function resetStore() {
  useConversationStore.setState({
    chats: new Map(),
    orderedIds: [],
    activeChatId: null,
    dropTtsUntilTurnEnd: false,
  });
  sendMessage.mockClear();
}

describe("conversation store — sendSteer (Q3 frontend)", () => {
  beforeEach(resetStore);

  it('sends WS kind "steer" with the resolved chat_id and trimmed text', () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.beginTurn(A, "1"); // turn is streaming

    st.sendSteer(A, "  actually do this instead  ");

    expect(sendMessage).toHaveBeenCalledWith("steer", {
      chat_id: A,
      text: "actually do this instead",
    });
  });

  it("renders an immediately-complete bubble flagged steered:true, never queued", () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.beginTurn(A, "1");

    st.sendSteer(A, "redirect this");

    const users = useConversationStore
      .getState()
      .getSlice(A)!
      .messages.filter((m) => m.role === "user");
    expect(users).toHaveLength(1);
    expect(users[0].content).toBe("redirect this");
    expect(users[0].status).toBe("complete");
    expect(users[0].steered).toBe(true);
    expect(users[0].queuePosition).toBeUndefined();
  });

  it("does nothing for a blank/whitespace-only steer", () => {
    const st = useConversationStore.getState();
    st.initChat(A);
    st.beginTurn(A, "1");

    st.sendSteer(A, "   ");

    expect(sendMessage).not.toHaveBeenCalled();
    expect(useConversationStore.getState().getSlice(A)!.messages).toHaveLength(
      0,
    );
  });

  it("does nothing when chatId cannot be resolved (no active chat)", () => {
    useConversationStore.getState().sendSteer(null, "hello");
    expect(sendMessage).not.toHaveBeenCalled();
  });
});
