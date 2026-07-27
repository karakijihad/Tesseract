import type {
  Envelope,
  SessionCompactData,
  SessionCompactFileData,
  SessionCreatedData,
  SessionDeletedData,
  SessionListData,
  SessionLoadedData,
  SessionSavedData,
  SessionStatsData,
  SoulUpdatedData,
} from "../../lib/types";
import { rehydrateHistory } from "../../lib/chatHistory";
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
        chat.hydrateChats(
          data.chats.map((c) => ({ chatId: c.chat_id, title: c.title })),
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
      useUIStore.getState().setDrawerOpen(true);
      break;
    }
    case "session_loaded": {
      const data = env.data as unknown as SessionLoadedData;
      const { messages, modelById, statsById } = rehydrateHistory(data.history);
      chat.loadHistory(env.chat_id ?? null, messages, { modelById, statsById });
      sessions.setSaveName(data.save_name);
      // Observer panels are per-session — drop the prior session's
      // suggestions/observations so the loaded session starts clean.
      useSuggestionsStore.getState().reset();
      useObservationsStore.getState().reset();
      toasts.push(`Loaded ${data.save_name}`);
      break;
    }
    case "session_saved": {
      const data = env.data as unknown as SessionSavedData;
      sessions.setSaveName(data.save_name);
      toasts.push(`Saved as ${data.save_name}`);
      break;
    }
    case "session_reset": {
      const data = env.data as
        | { autosaved?: boolean; save_name?: string | null }
        | undefined;
      chat.reset(env.chat_id ?? null);
      sessions.setSaveName(null);
      useSuggestionsStore.getState().reset();
      useObservationsStore.getState().reset();
      useTasksStore.getState().reset();
      toasts.push(
        data?.autosaved && data.save_name
          ? `Session reset · saved as ${data.save_name}`
          : "Session reset",
      );
      break;
    }
    case "session_compact": {
      const data = env.data as unknown as SessionCompactData;
      const tag = data.trigger === "auto" ? "Auto-compacted" : "Compacted";
      toasts.push(`${tag} ${data.tokens_before} → ${data.tokens_after} tok`);
      break;
    }
    case "session_stats": {
      const data = env.data as unknown as SessionStatsData;
      sessions.setLatestStats(data);
      const ui = useUIStore.getState();
      if (ui.pendingStatsToast) {
        const tokK = (data.tokens / 1000).toFixed(1);
        const capK = (data.compact_threshold_tokens / 1000).toFixed(1);
        const pct = Math.round(
          (data.tokens / data.compact_threshold_tokens) * 100,
        );
        toasts.push(
          `Stats: ${data.turns} turns · ${tokK}k / ${capK}k tok (${pct}%)`,
        );
        ui.setPendingStatsToast(false);
      }
      break;
    }
    case "session_deleted": {
      const data = env.data as unknown as SessionDeletedData;
      if (sessions.saveName === data.save_name) {
        sessions.setSaveName(null);
      }
      sessions.fetchList();
      toasts.push(`Deleted ${data.save_name}`);
      break;
    }
    case "session_compact_file": {
      const data = env.data as unknown as SessionCompactFileData;
      sessions.fetchList();
      toasts.push(
        `Compacted ${data.save_name}: ${data.tokens_before} → ${data.tokens_after} tok`,
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
