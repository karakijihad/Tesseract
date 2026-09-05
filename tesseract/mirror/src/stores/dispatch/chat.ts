import type { Envelope, RawHistoryEntry } from "../../lib/types";
import { rehydrateHistory } from "../../lib/chatHistory";
import { getTtsPlayer } from "../../lib/voice/tts-player";
import { useConversationStore } from "../conversation";
import { useSessionStore } from "../session";
import { useTasksStore } from "../tasks";
import { useToastStore } from "../toasts";

// Every focus transition (create / switch / archive / restore) has to put down
// what belonged to the chat being left. Centralized so a new focus-changing
// envelope cannot silently reintroduce either leak.
//
// Audio: the outgoing chat's queued speech, or old-chat voice trails on across
// the switch.
//
// The checklist: each chat has its own ChatSession and its own
// `tool_context.todos`, so a list left on screen is another conversation's
// work, described as this one's. Cleared rather than swapped, because the
// switch frame does not carry the incoming chat's list; the next `tasks_state`
// from the chat that is now in focus fills it back in.
function _onFocusChange(newActiveId: string | undefined): void {
  if (!newActiveId) return;
  if (useConversationStore.getState().activeChatId !== newActiveId) {
    getTtsPlayer().cancel();
    useTasksStore.getState().reset();
  }
}

/** What an unstamped message in a rehydrated history is dated against.
 *
 * Per-message timestamps are recent. A record written before them carries
 * none, and `rehydrateHistory` falls back to `Date.now()` for those, which
 * dated a conversation from March as though it had just happened and filed it
 * under today in the rail the moment it was opened. The chat's own creation
 * stamp is the honest answer, and the backend's own rule agrees: with nothing
 * stamped, `chat_store._last_active_stamp` falls back to the record fields.
 *
 * `undefined` when the stamp is missing or unparseable, which leaves
 * `rehydrateHistory` on its own default rather than passing it a NaN.
 */
function _bornAt(createdAt: string | undefined): number | undefined {
  if (!createdAt) return undefined;
  const parsed = Date.parse(createdAt);
  return Number.isNaN(parsed) ? undefined : parsed;
}

export function handleChat(env: Envelope): void {
  const chat = useConversationStore.getState();
  const data = env.data as {
    chat_id?: string;
    title?: string;
    created_at?: string;
    history?: RawHistoryEntry[];
    active_chat_id?: string;
    reason?: string;
  };
  switch (env.type) {
    case "chat_created": {
      if (!data.chat_id) break;
      _onFocusChange(data.chat_id); // create focuses the new chat
      chat.initChat(data.chat_id); // creates + makes active
      if (data.title) chat.setChatTitle(data.chat_id, data.title);
      // The open-chat store is the only place the stamp comes from, because
      // the row that carries it has not been fetched yet.
      if (data.created_at) chat.setChatCreatedAt(data.chat_id, data.created_at);
      // The backend persists the record before it sends this, and the rail
      // only refetches when it opens or changes tab. Without this the new
      // chat is on disk, counted as running, and missing from History until
      // the operator toggles to Archive and back.
      void useSessionStore.getState().fetchList();
      break;
    }
    case "chat_switched": {
      if (!data.chat_id) break;
      // Put down the outgoing chat's audio and checklist the moment focus
      // moves; the incoming chat fills both back in from its own turns.
      _onFocusChange(data.chat_id);
      chat.initChat(data.chat_id); // idempotent — sets active
      // loadHistory replaces the slice wholesale, so set the title AFTER it.
      const { messages, modelById, statsById } = rehydrateHistory(
        data.history ?? [],
        _bornAt(data.created_at),
      );
      chat.loadHistory(data.chat_id, messages, { modelById, statsById });
      if (data.title) chat.setChatTitle(data.chat_id, data.title);
      break;
    }
    case "chat_archived": {
      if (!data.chat_id) break;
      // Archiving the active chat moves focus away, so it puts its audio and
      // its checklist down too.
      _onFocusChange(data.active_chat_id);
      chat.archiveChat(data.chat_id);
      // Backend reports the new active chat (archive switches active away);
      // follow it authoritatively rather than trusting the local fallback.
      if (data.active_chat_id) chat.initChat(data.active_chat_id);
      // Archiving from anywhere but the rail leaves both of its lists stale.
      void useSessionStore.getState().fetchList();
      void useSessionStore.getState().fetchArchive();
      break;
    }
    case "chat_renamed": {
      if (!data.chat_id || !data.title) break;
      chat.setChatTitle(data.chat_id, data.title);
      // A rename makes a chat worth a file — `chat_store._is_disposable` keeps
      // a blank one off disk only while its title is still the birth stamp —
      // and `_handle_chat_rename` persists it there and then. So the library
      // has a row this connection has not seen, and the rail was still drawing
      // its own synthesised row for the chat: that row labels itself with the
      // first thing typed, which then outranked the name the operator had just
      // chosen. The other three focus verbs in this switch already refetch;
      // this one was the omission.
      void useSessionStore.getState().fetchList();
      break;
    }
    case "chat_restored": {
      // P5 — un-archived back into the open set + focused. Same shape as
      // chat_switched: initChat adds the tab (orderedIds) + makes it active,
      // loadHistory replaces the slice, title set after.
      if (!data.chat_id) break;
      _onFocusChange(data.chat_id);
      chat.initChat(data.chat_id);
      const { messages, modelById, statsById } = rehydrateHistory(
        data.history ?? [],
        _bornAt(data.created_at),
      );
      chat.loadHistory(data.chat_id, messages, { modelById, statsById });
      if (data.title) chat.setChatTitle(data.chat_id, data.title);
      void useSessionStore.getState().fetchList();
      void useSessionStore.getState().fetchArchive();
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
