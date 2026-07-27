import { describe, it, expect, beforeEach, vi } from "vitest";

// dispatch.ts transitively opens a socket via the websocket store; stub it.
vi.mock("./websocket", () => ({
  useWebSocketStore: {
    getState: () => ({ sendMessage: vi.fn(), setSessionId: vi.fn() }),
  },
}));

// M10 — spy on the TTS player so we can assert queued audio is cancelled on
// every focus transition (create / switch / archive / restore).
const { ttsCancel } = vi.hoisted(() => ({ ttsCancel: vi.fn() }));
vi.mock("../lib/voice/tts-player", () => ({
  getTtsPlayer: () => ({ cancel: ttsCancel }),
}));

import { handleEnvelope } from "./dispatch";
import { useConversationStore } from "./conversation";
import { useToastStore } from "./toasts";
import type { Envelope } from "../lib/types";

const A = "a".repeat(32);
const B = "b".repeat(32);

function resetStore() {
  useConversationStore.setState({
    chats: new Map(),
    orderedIds: [],
    activeChatId: null,
    dropTtsUntilTurnEnd: false,
  });
}

// Catchup mode skips the pulse/controller side-effects — pure routing to the
// category handler.
function chatEnv(type: string, data: Record<string, unknown>): Envelope {
  return {
    type,
    category: "chat",
    session_id: "s",
    timestamp: "2026-06-29T00:00:00Z",
    data,
  } as Envelope;
}

function dispatch(type: string, data: Record<string, unknown>): void {
  handleEnvelope(chatEnv(type, data), { fromCatchup: true });
}

describe("dispatch — focus-transition audio cancel (M10)", () => {
  beforeEach(() => {
    resetStore();
    ttsCancel.mockClear();
  });

  it("cancels outgoing audio when a NEW chat is created (focus moves)", () => {
    dispatch("chat_created", { chat_id: A, title: "A" }); // active A
    ttsCancel.mockClear();
    dispatch("chat_created", { chat_id: B, title: "B" }); // focus → B
    expect(ttsCancel).toHaveBeenCalledTimes(1);
  });

  it("cancels outgoing audio when the active chat is archived", () => {
    dispatch("chat_created", { chat_id: A, title: "A" });
    dispatch("chat_created", { chat_id: B, title: "B" }); // active B
    ttsCancel.mockClear();
    dispatch("chat_archived", { chat_id: B, active_chat_id: A }); // focus → A
    expect(ttsCancel).toHaveBeenCalledTimes(1);
  });

  it("cancels outgoing audio on switch and restore", () => {
    dispatch("chat_created", { chat_id: A, title: "A" });
    dispatch("chat_created", { chat_id: B, title: "B" }); // active B
    ttsCancel.mockClear();
    dispatch("chat_switched", { chat_id: A, history: [] }); // focus → A
    dispatch("chat_restored", { chat_id: B, history: [] }); // focus → B
    expect(ttsCancel).toHaveBeenCalledTimes(2);
  });

  it("does NOT cancel when the target is already active (no focus change)", () => {
    dispatch("chat_created", { chat_id: A, title: "A" }); // active A
    ttsCancel.mockClear();
    dispatch("chat_switched", { chat_id: A, history: [] }); // already active
    expect(ttsCancel).not.toHaveBeenCalled();
  });

  it("does NOT cancel active audio when a BACKGROUND chat is archived", () => {
    dispatch("chat_created", { chat_id: A, title: "A" });
    dispatch("chat_created", { chat_id: B, title: "B" }); // active B
    ttsCancel.mockClear();
    // Archiving the non-active A: backend keeps active = B, so no focus move.
    dispatch("chat_archived", { chat_id: A, active_chat_id: B });
    expect(ttsCancel).not.toHaveBeenCalled();
  });
});

describe("dispatch — chat lifecycle (P3 inc.2)", () => {
  beforeEach(resetStore);

  it("chat_created registers the chat, sets its title, and makes it active", () => {
    dispatch("chat_created", {
      chat_id: A,
      title: "2026-06-29 10:00",
      created_at: "x",
    });
    const s = useConversationStore.getState();
    expect(s.getSlice(A)).not.toBeNull();
    expect(s.getSlice(A)?.title).toBe("2026-06-29 10:00");
    expect(s.activeChatId).toBe(A);
    expect(s.orderedIds).toEqual([A]);
  });

  it("chat_switched makes the chat active and sets its title", () => {
    dispatch("chat_created", { chat_id: A, title: "A" });
    dispatch("chat_created", { chat_id: B, title: "B" }); // active B now
    dispatch("chat_switched", { chat_id: A, title: "A renamed", history: [] });
    const s = useConversationStore.getState();
    expect(s.activeChatId).toBe(A);
    expect(s.getSlice(A)?.title).toBe("A renamed");
  });

  it("chat_archived removes the chat and follows the backend active_chat_id", () => {
    dispatch("chat_created", { chat_id: A, title: "A" });
    dispatch("chat_created", { chat_id: B, title: "B" }); // orderedIds [B, A], active B
    dispatch("chat_archived", { chat_id: B, active_chat_id: A });
    const s = useConversationStore.getState();
    expect(s.getSlice(B)).toBeNull();
    expect(s.activeChatId).toBe(A);
    expect(s.orderedIds).toEqual([A]);
  });

  it("session_created hydrates the full open-chat set (reload)", () => {
    handleEnvelope(
      {
        type: "session_created",
        category: "session",
        session_id: "s",
        timestamp: "2026-06-29T00:00:00Z",
        data: {
          session_id: "s",
          started_at: "t",
          active_chat_id: A,
          chats: [
            { chat_id: B, title: "Newer" },
            { chat_id: A, title: "Active" },
          ],
        },
      } as Envelope,
      { fromCatchup: true },
    );
    const s = useConversationStore.getState();
    expect(s.orderedIds).toEqual([B, A]);
    expect(s.activeChatId).toBe(A);
    expect(s.getSlice(A)?.title).toBe("Active");
    expect(s.getSlice(B)?.title).toBe("Newer");
  });

  it("chat_renamed updates the chat title in place", () => {
    dispatch("chat_created", { chat_id: A, title: "old" });
    dispatch("chat_renamed", { chat_id: A, title: "Vault notes" });
    expect(useConversationStore.getState().getSlice(A)?.title).toBe(
      "Vault notes",
    );
  });

  it("chat_restored re-adds the tab with its history and makes it active", () => {
    dispatch("chat_created", { chat_id: A, title: "A" }); // active A
    dispatch("chat_restored", {
      chat_id: B,
      title: "Restored",
      active_chat_id: B,
      history: [{ role: "user", content: "old message" }],
    });
    const s = useConversationStore.getState();
    expect(s.orderedIds).toContain(B);
    expect(s.activeChatId).toBe(B);
    expect(s.getSlice(B)?.title).toBe("Restored");
    expect(s.getSlice(B)?.messages.at(-1)?.content).toBe("old message");
  });

  it("chat_create_failed surfaces a toast and leaves the store untouched", () => {
    const push = vi.spyOn(useToastStore.getState(), "push");
    dispatch("chat_create_failed", { reason: "infra_not_ready" });
    expect(push).toHaveBeenCalled();
    expect(useConversationStore.getState().chats.size).toBe(0);
    push.mockRestore();
  });
});
