import type { CommandResultData, Envelope } from "../../lib/types";
import { useToastStore } from "../toasts";
import { setOrbState } from "./orb";

export function handleCommand(env: Envelope): void {
  // Pre-run + kernel-tool result envelopes for the unified slash dispatch
  // (see `commands_registry.py`). `command_running` is a transient
  // acknowledgement so the operator knows the slash fired before any
  // handler-specific result envelope (e.g. `session_reset`, `reflect_result`)
  // arrives. `tool_slash_result` is the printable output of a kernel-tool
  // slash invocation — there is no Mirror-specific envelope for those, so
  // we render the formatted string here.
  const toasts = useToastStore.getState();
  if (env.type === "command_running") {
    const data = env.data as { name?: string; head?: string } | undefined;
    const head = data?.head?.replace(/^\//, "") || data?.name || "command";
    toasts.push(`/${head} running…`, "info");
    return;
  }
  if (env.type === "tool_slash_result") {
    const data = env.data as
      | { name?: string; output?: string; is_error?: boolean }
      | undefined;
    const out = (data?.output || "").trim();
    if (!out) return;
    const truncated = out.length > 320 ? out.slice(0, 320) + "…" : out;
    toasts.push(
      `/${data?.name}: ${truncated}`,
      data?.is_error ? "error" : "info",
    );
    return;
  }
  console.debug("[dispatch] unhandled command type:", env.type);
}

export function handleCommandResult(env: Envelope): void {
  const data = env.data as unknown as CommandResultData;
  if (data.ok) return;
  const toasts = useToastStore.getState();
  if (data.severity === "warning") {
    // Operator-recoverable failure (e.g. typo). Toast only — DO NOT call
    // entity.setState('error') so the orb stays normal. Phase F2 obs #5.
    toasts.push(data.reason, "warning");
    return;
  }
  toasts.push(data.reason, "error");
  setOrbState("error");
}
