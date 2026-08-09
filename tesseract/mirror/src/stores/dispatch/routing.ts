import type {
  Envelope,
  IdentityChangedData,
  ModeChangedData,
  ModelSelectedData,
} from "../../lib/types";
import { useConversationStore } from "../conversation";
import { useIdentityStore } from "../identity";
import { usePulseStore } from "../pulse";
import { useToastStore } from "../toasts";
import { useToolsStore } from "../tools";

export function handleRouting(env: Envelope): void {
  const chat = useConversationStore.getState();
  const cid = env.chat_id ?? null;
  const identity = useIdentityStore.getState();
  const toasts = useToastStore.getState();

  switch (env.type) {
    case "mode_changed": {
      const data = env.data as unknown as ModeChangedData;
      identity.setSecurityMode(data.to);
      toasts.push(`Mode: ${data.from} → ${data.to}`);
      // Tool postures are mode-aware; force the Settings tools view to refetch
      // so it reflects the new policy instead of stale defaults.
      useToolsStore.getState().invalidate();
      break;
    }
    case "identity_changed": {
      const data = env.data as unknown as IdentityChangedData;
      identity.setNames(data.name, data.operator_name);
      // Broadcast to every open session, including the one that saved —
      // a second cockpit window must not keep rendering the old name.
      toasts.push(`Identity: ${data.name}`);
      break;
    }
    case "model_selected": {
      const data = env.data as unknown as ModelSelectedData;
      chat.setMessageModel(cid, data);
      if (data.role === "chat_brain")
        identity.setModel(data.provider, data.model);
      // Surface fallback in the pulse so the operator can see "primary
      // failed, secondary answered" without having to read backend logs.
      // Regular model_selected (primary committed) does NOT push — it
      // would dominate the feed with one row per turn.
      if (data.is_fallback) {
        usePulseStore.getState().push(env);
        const primaryLabel = data.primary
          ? `${data.primary.provider}/${data.primary.model}`
          : "primary";
        const reason = data.fallback_reason ? ` — ${data.fallback_reason}` : "";
        useToastStore
          .getState()
          .push(
            `Fell back from ${primaryLabel} to ${data.provider}/${data.model}${reason}`,
            "warning",
          );
      }
      break;
    }
    default:
      console.debug("[dispatch] unhandled routing type:", env.type);
  }
}
