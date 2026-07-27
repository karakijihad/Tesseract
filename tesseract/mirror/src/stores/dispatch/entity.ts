import type {
  CodeDriftDetectedData,
  ConfigReloadedData,
  Envelope,
  EntitySignalsData,
  EntityStateSetData,
} from "../../lib/types";
import { useCodeDriftStore } from "../codeDrift";
import { useConversationStore } from "../conversation";
import { useEntityStore } from "../entity";
import { useOrbVisibilityStore } from "../orbVisibility";
import { useToastStore } from "../toasts";
import { resetOrbDwellForServerWrite } from "./orb";
import type { Signals } from "./signals";

export function handleEntity(env: Envelope, signals: Signals | null): void {
  if (env.type === "entity_state_set") {
    const data = env.data as unknown as EntityStateSetData;
    // Server-authoritative write — flush any queued dwell flip so a
    // late-firing timer can't clobber the state we're about to set.
    resetOrbDwellForServerWrite();
    useEntityStore.getState().setState(data.state);
    return;
  }
  if (env.type === "config_reloaded") {
    const data = env.data as unknown as ConfigReloadedData;
    const body = `${data.file} — ${data.summary}`;
    useToastStore.getState().push(body, data.ok ? "info" : "error");
    return;
  }
  if (env.type === "code_drift_detected") {
    const data = env.data as unknown as CodeDriftDetectedData;
    const count = data.paths.length;
    useCodeDriftStore.getState().pushDrift({
      classification: data.classification,
      paths: data.paths,
      headSha: data.head_sha,
      detectedAt: env.timestamp,
    });
    // One short transient nudge per drift envelope; the toast store's
    // prefix dedup folds bursts into "(xN)". The chip menu is the
    // durable surface — it carries the full timestamped history.
    const kind: "info" | "warning" =
      data.classification === "restart_required" ? "warning" : "info";
    const what =
      data.classification === "restart_required"
        ? `Backend drift (${count} file${count === 1 ? "" : "s"})`
        : `Frontend edits (${count} file${count === 1 ? "" : "s"})`;
    useToastStore.getState().push(`${what} — see code chip`, kind);
    return;
  }
  if (env.type === "orb_visibility") {
    // TARS-driven show/hide (orb_visibility tool) — same store the HUD
    // toggle writes, so the two controls can't fight over divergent state.
    const data = env.data as { visible?: boolean } | undefined;
    if (typeof data?.visible === "boolean") {
      useOrbVisibilityStore.getState().setVisible(data.visible);
    }
    return;
  }
  if (env.type === "chat_assistant_initiated") {
    // TARS speaking first — no inbound operator turn preceded this. The
    // backend `chat_initiate` tool pushed this envelope to every open
    // session so the chat tab renders the message as an entity-role
    // bubble. Reason rides through for filtering in future tooling.
    const data = env.data as { text?: string; reason?: string } | undefined;
    const text = (data?.text ?? "").toString().trim();
    if (!text) return;
    useConversationStore.getState().addEntityMessage(env.chat_id ?? null, text);
    return;
  }
  if (env.type !== "entity_signals" || signals === null) return;
  signals.ingestBackend(env.data as unknown as EntitySignalsData);
}
