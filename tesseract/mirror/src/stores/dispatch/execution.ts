import type {
  Envelope,
  ToolApprovedData,
  ToolAskData,
  ToolAutoData,
  ToolDeniedData,
  ToolDeniedHardData,
} from "../../lib/types";
import { useConversationStore } from "../conversation";

export function handleExecution(env: Envelope): void {
  const chat = useConversationStore.getState();
  const cid = env.chat_id ?? null;

  switch (env.type) {
    case "tool_ask": {
      const data = env.data as unknown as ToolAskData;
      chat.addApproval(cid, {
        call_id: data.call_id,
        name: data.name,
        input: data.input ?? {},
        reason: data.reason ?? "",
        received_at: Date.now(),
        resolved: false,
      });
      chat.setToolStatus(
        cid,
        data.call_id,
        "pending",
        data.reason || undefined,
      );
      break;
    }
    case "tool_approved": {
      const data = env.data as unknown as ToolApprovedData;
      chat.clearApproval(cid, data.call_id);
      chat.setToolStatus(cid, data.call_id, "approved");
      break;
    }
    case "tool_denied": {
      const data = env.data as unknown as ToolDeniedData;
      chat.clearApproval(cid, data.call_id);
      // Forward the reason so the chat row can render "turn cancelled"
      // distinctly from a plain operator "no". `setToolStatus` already
      // accepts an optional reason string (used by the `pending` case
      // above for the original ask reason).
      chat.setToolStatus(cid, data.call_id, "denied", data.reason || undefined);
      break;
    }
    case "tool_ask_parked": {
      // trio W4 — the ask's live window expired for a background spawn; the
      // question moved to the approvals pane. Flip the pill so the card
      // reads parked, not eternally pending. The eventual tool_approved /
      // tool_denied envelope flips it again when the operator decides.
      const data = env.data as unknown as { call_id: string };
      chat.setToolStatus(cid, data.call_id, "parked", "answer in Approvals");
      break;
    }
    case "tool_auto": {
      const data = env.data as unknown as ToolAutoData;
      chat.setToolStatus(cid, data.call_id, "auto");
      break;
    }
    case "tool_denied_hard": {
      const data = env.data as unknown as ToolDeniedHardData;
      chat.setToolStatus(cid, data.call_id, "hard_denied", data.reason);
      break;
    }
    default:
      console.debug("[dispatch] unhandled execution type:", env.type);
  }
}
