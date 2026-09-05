import type {
  CliEndData,
  CliOutputData,
  CliStartData,
  Envelope,
} from "../../lib/types";
import { getController } from "../../lib/entity/registry";
import { useConversationStore } from "../conversation";
import { useEntityStore } from "../entity";
import { useToolActivityStore } from "../toolActivity";
import { setOrbState } from "./orb";

export function handleCli(env: Envelope): void {
  const chat = useConversationStore.getState();
  const cid = env.chat_id ?? null;

  switch (env.type) {
    case "cli_start": {
      const data = env.data as unknown as CliStartData;
      chat.startCli(cid, data.call_id, data.tool);
      setOrbState("spawning");
      // Count on the id the tool-call event carried, which is the stream's
      // own id for a delegate and the originating call for a sub-agent whose
      // card is bound to its spawn handle. The store counts an id once.
      useToolActivityStore
        .getState()
        .setLastTool(data.tool, data.origin_call_id ?? data.call_id);
      break;
    }
    case "cli_output": {
      const data = env.data as unknown as CliOutputData;
      chat.appendCliLine(cid, data.call_id, data.delta);
      break;
    }
    case "cli_end": {
      const data = env.data as unknown as CliEndData;
      chat.endCli(cid, data.call_id, data.exit_code);
      // Re-read live state; the function-top snapshot may be stale
      // after any earlier `setOrbState` in this same task.
      if (useEntityStore.getState().state === "spawning")
        setOrbState("thinking");
      if (data.exit_code === 0) getController()?.pulseEvent("cli_ok");
      break;
    }
    default:
      console.debug("[dispatch] unhandled cli type:", env.type);
  }
}
