import type { Envelope, RawHistoryEntry } from "../../lib/types";
import { rehydrateHistory } from "../../lib/chatHistory";
import { getTtsPlayer } from "../../lib/voice/tts-player";
import { useConversationStore } from "../conversation";
import { useToastStore } from "../toasts";

// parallel-tars P6 (M10) — every focus transition (create / switch / archive /
// restore) must stop the outgoing chat's queued audio the moment focus leaves
// it, or old-chat speech trails on across the switch. Centralized so a new
// focus-changing envelope can't silently reintroduce the leak.
function _cancelOutgoingAudioIfFocusChanges(
  newActiveId: string | undefined,
): void {
  if (!newActiveId) return;
  if (useConversationStore.getState().activeChatId !== newActiveId) {
    getTtsPlayer().cancel();
  }
}

export function handleChat(env: Envelope): void {
  const chat = useConversationStore.getState();
  const data = env.data as {
    chat_id?: string;
    title?: string;
    history?: RawHistoryEntry[];
    active_chat_id?: string;
    reason?: string;
  };
  switch (env.type) {
    case "chat_created": {
      if (!data.chat_id) break;
      _cancelOutgoingAudioIfFocusChanges(data.chat_id); // create focuses the new chat
      chat.initChat(data.chat_id); // creates + makes active
      if (data.title) chat.setChatTitle(data.chat_id, data.title);
      break;
    }
    case "chat_switched": {
      if (!data.chat_id) break;
      // parallel-tars P6 — stop the outgoing chat's audio the moment focus
      // moves; the incoming chat's voice picks up from its next chunk.
      _cancelOutgoingAudioIfFocusChanges(data.chat_id);
      chat.initChat(data.chat_id); // idempotent — sets active
      // loadHistory replaces the slice wholesale, so set the title AFTER it.
      const { messages, modelById, statsById } = rehydrateHistory(
        data.history ?? [],
      );
      chat.loadHistory(data.chat_id, messages, { modelById, statsById });
      if (data.title) chat.setChatTitle(data.chat_id, data.title);
      break;
    }
    case "chat_archived": {
      if (!data.chat_id) break;
      // Archiving the active chat moves focus away — stop its queued audio.
      _cancelOutgoingAudioIfFocusChanges(data.active_chat_id);
      chat.archiveChat(data.chat_id);
      // Backend reports the new active chat (archive switches active away);
      // follow it authoritatively rather than trusting the local fallback.
      if (data.active_chat_id) chat.initChat(data.active_chat_id);
      break;
    }
    case "chat_renamed": {
      if (data.chat_id && data.title)
        chat.setChatTitle(data.chat_id, data.title);
      break;
    }
    case "chat_restored": {
      // P5 — un-archived back into the open set + focused. Same shape as
      // chat_switched: initChat adds the tab (orderedIds) + makes it active,
      // loadHistory replaces the slice, title set after.
      if (!data.chat_id) break;
      _cancelOutgoingAudioIfFocusChanges(data.chat_id);
      chat.initChat(data.chat_id);
      const { messages, modelById, statsById } = rehydrateHistory(
        data.history ?? [],
      );
      chat.loadHistory(data.chat_id, messages, { modelById, statsById });
      if (data.title) chat.setChatTitle(data.chat_id, data.title);
      break;
    }
    case "chat_create_failed":
    case "chat_switch_failed":
    case "chat_archive_failed":
    case "chat_restore_failed":
    case "chat_rename_failed": {
      useToastStore
        .getState()
        .push(`Chat action failed: ${data.reason ?? "unknown"}`, "warning");
      break;
    }
    default:
      console.debug("[dispatch] unhandled chat type:", env.type);
  }
}
