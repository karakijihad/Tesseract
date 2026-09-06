import type {
  Envelope,
  SessionCompactData,
  SessionCompactFileData,
  SessionCreatedData,
  SessionDeletedData,
  SessionListData,
  SessionSavedData,
  SessionStatsData,
  SoulUpdatedData,
} from "../../lib/types";
import { foldCeiling } from "../../lib/types";
import { useChannelsStore } from "../channels";
import { useConversationStore } from "../conversation";
import { useEntityStore } from "../entity";
import { useObservationsStore } from "../observations";
import { useSessionStore } from "../session";
import { useSoulStore } from "../soul";
import { useSuggestionsStore } from "../suggestions";
import { useTasksStore } from "../tasks";
import { useToastStore } from "../toasts";
import { useUIStore } from "../ui";
import { useWebSocketStore } from "../websocket";
import { useWorkspaceStore } from "../workspace";

export function handleSession(env: Envelope): void {
  const chat = useConversationStore.getState();
  const sessions = useSessionStore.getState();
  const toasts = useToastStore.getState();

  switch (env.type) {
    case "session_created": {
      const data = env.data as unknown as SessionCreatedData;
      useWebSocketStore.getState().setSessionId(data.session_id);
      // mirror-multi-chat P3 — rehydrate the whole open-chat set so the tab
      // strip survives a reload. Falls back to seeding just the active slice
      // (inc.B) when the backend doesn't send the list (older backend).
      if (data.chats && data.chats.length > 0) {
        // `created_at` is passed through as-is, undefined included: the
        // store treats an absent stamp as "keep the one you hold", and
        // coalescing to "" here would blank it instead.
        chat.hydrateChats(
          data.chats.map((c) => ({
            chatId: c.chat_id,
            title: c.title,
            createdAt: c.created_at,
          })),
          data.active_chat_id,
        );
      } else {
        chat.initChat(data.active_chat_id);
      }
      useEntityStore.getState().setState("idle");
      break;
    }
    case "session_list": {
      const data = env.data as unknown as SessionListData;
      sessions.setSessionList(data);
      useUIStore.getState().setChatRailOpen(true);
      break;
    }
    case "session_saved": {
      const data = env.data as unknown as SessionSavedData;
      sessions.setLastChatId(data.chat_id);
      toasts.push(`Saved as ${data.title}`);
      break;
    }
    case "session_reset": {
      const data = env.data as
        | { autosaved?: boolean; chat_id?: string | null; title?: string | null }
        | undefined;
      chat.reset(env.chat_id ?? null);
      // The conversation that was reset is archived, not gone — but it is not
      // the one to auto-resume into either. The fresh chat becomes the target
      // on its first save.
      sessions.setLastChatId(null);
      useSuggestionsStore.getState().reset();
      useObservationsStore.getState().reset();
      useTasksStore.getState().reset();
      toasts.push(
        data?.autosaved && data.title
          ? `New conversation · ${data.title} archived`
          : "Session reset",
      );
      break;
    }
    case "session_note": {
      // A sentence the boundary produced, drawn where it happened rather than
      // as a toast that is gone in five seconds. A reloaded conversation gets
      // the same thing back out of history (`lib/chatHistory.ts`), so this is
      // only the live half.
      const data = env.data as { text?: string; mark?: string } | undefined;
      if (data?.text) {
        chat.addRuntimeNote(env.chat_id ?? null, data.text, data.mark ?? "boundary");
      }
      break;
    }
    case "session_compact": {
      const data = env.data as unknown as SessionCompactData;
      const tag = data.trigger === "auto" ? "Auto-compacted" : "Compacted";
      toasts.push(`${tag} ${data.tokens_before} → ${data.tokens_after} tok`);
      // The toast is gone in five seconds; the divider stays where it
      // happened. The backend stamps the chat that folded, which is not
      // necessarily the one on screen when a background turn compacts, and
      // says how many turns it kept verbatim so the divider lands in front of
      // them rather than at the end.
      chat.addFoldMarker(env.chat_id ?? null, data.tail_turns ?? 0);
      break;
    }
    case "session_stats": {
      const data = env.data as unknown as SessionStatsData;
      // A conductor fan-out emits stats for chats nobody is looking at, and
      // the Settings bar reads this store for its measured floor. Keeping a
      // background chat's numbers would draw one conversation's shape while
      // naming another's. Unstamped envelopes are session-scoped and stand.
      const activeChatId = useConversationStore.getState().activeChatId;
      if (env.chat_id == null || env.chat_id === activeChatId) {
        sessions.setLatestStats(data, env.chat_id ?? activeChatId);
      }
      const ui = useUIStore.getState();
      if (ui.pendingStatsToast) {
        const ceiling = foldCeiling(data);
        const tokK = (data.tokens / 1000).toFixed(1);
        const capK = (ceiling / 1000).toFixed(1);
        const pct = ceiling ? Math.round((data.tokens / ceiling) * 100) : 0;
        toasts.push(
          `Stats: ${data.turns} turns · ${tokK}k / ${capK}k tok (${pct}%)`,
        );
        ui.setPendingStatsToast(false);
      }
      break;
    }
    case "session_deleted": {
      const data = env.data as unknown as SessionDeletedData;
      if (sessions.lastChatId === data.chat_id) {
        sessions.setLastChatId(null);
      }
      sessions.fetchList();
      toasts.push(`Deleted ${data.title}`);
      break;
    }
    case "session_compact_file": {
      const data = env.data as unknown as SessionCompactFileData;
      sessions.fetchList();
      toasts.push(
        `Compacted ${data.title}: ${data.tokens_before} → ${data.tokens_after} tok`,
      );
      break;
    }
    case "soul_updated": {
      const data = env.data as unknown as SoulUpdatedData;
      const soul = useSoulStore.getState();
      soul.setContent(data.content);
      if (data.last_reflected_at) {
        soul.setLastReflectedAt(data.last_reflected_at);
      }
      break;
    }
    case "reflect_result": {
      // Update the "last reflected" chip even when SOUL.md didn't change
      // — reflection happened, the chip should reset to "just now".
      const data = env.data as { last_reflected_at?: string };
      if (data.last_reflected_at) {
        useSoulStore.getState().setLastReflectedAt(data.last_reflected_at);
      }
      break;
    }
    case "log_error": {
      // MO-9-11 — fan a copy into the Channels store so the LogsPane
      // surfaces per-channel error tails. The pulse store already
      // received this envelope above (every envelope path goes through
      // `usePulseStore.push`); the channel filter happens inside the
      // store so unrelated `log_error` rows are cheap no-ops.
      useChannelsStore.getState().applyEnvelope(env);
      break;
    }
    case "workspace_file_updated": {
      // Generic notice that a workspace .md file (IDENTITY/FOUNDATION/…)
      // was just committed via workspace_decision. SOUL.md uses the
      // dedicated `soul_updated` envelope above. No specific viewer
      // refresh is wired today; the inbox row's status flips on the
      // REST response. Refetch the inbox so the operator sees the
      // settled state without manual reload.
      useWorkspaceStore.getState().fetchInbox();
      break;
    }
    default:
      console.debug("[dispatch] unhandled session type:", env.type);
  }
}
