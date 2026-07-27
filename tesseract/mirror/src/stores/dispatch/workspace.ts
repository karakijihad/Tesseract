import type { Envelope } from "../../lib/types";
import { useWorkspaceStore, type WorkspaceEvent } from "../workspace";

export function handleWorkspace(env: Envelope): void {
  // Live-push for inbox writes from the backend (reflection_proposal,
  // dream_cycle nudges). Without this the operator only sees new rows
  // after a manual Refresh or after switching tabs. The InboxPanel store
  // already has `upsertEvent` + the unread-badge derivation — we feed
  // the envelope's payload through the same path a fetch would take.
  if (env.type === "workspace_comment_appended") {
    const raw = env.data as
      | Partial<{
          comment_id: string;
          event_id: string;
          ts: string;
          author: string;
          body: string;
          reply_to: string | null;
          delivered_to_tars: boolean;
        }>
      | undefined;
    if (
      !raw ||
      typeof raw.comment_id !== "string" ||
      typeof raw.event_id !== "string" ||
      typeof raw.author !== "string" ||
      typeof raw.body !== "string"
    ) {
      console.warn(
        "[dispatch] workspace_comment_appended: malformed envelope",
        env.data,
      );
      return;
    }
    useWorkspaceStore.getState().appendComment({
      comment_id: raw.comment_id,
      event_id: raw.event_id,
      ts: raw.ts ?? new Date().toISOString(),
      author: raw.author as "operator" | "tars",
      body: raw.body,
      reply_to: raw.reply_to ?? null,
      delivered_to_tars: raw.delivered_to_tars ?? false,
    });
    return;
  }
  if (env.type === "workspace_thread_pending") {
    const raw = env.data as
      | Partial<{
          event_id: string;
          comment_id: string;
          state: string;
        }>
      | undefined;
    if (
      !raw ||
      typeof raw.event_id !== "string" ||
      typeof raw.comment_id !== "string" ||
      typeof raw.state !== "string"
    ) {
      console.warn(
        "[dispatch] workspace_thread_pending: malformed envelope",
        env.data,
      );
      return;
    }
    const set = useWorkspaceStore.getState().setThreadPending;
    if (raw.state === "cleared") {
      set(raw.event_id, null);
    } else if (raw.state === "queued" || raw.state === "thinking") {
      set(raw.event_id, { comment_id: raw.comment_id, state: raw.state });
    } else {
      console.warn(
        "[dispatch] workspace_thread_pending: unknown state",
        raw.state,
      );
    }
    return;
  }
  if (env.type !== "workspace_event_appended") {
    console.debug("[dispatch] unhandled workspace type:", env.type);
    return;
  }
  const raw = env.data as Partial<WorkspaceEvent> | undefined;
  // Schema-drift guard: validate every required scalar before casting.
  // A partial backend write or new required field would otherwise reach
  // `upsertEvent` as a structurally-broken object that downstream filters
  // (`e.ts > lastSeen`, `e.kind` switch) silently mishandle.
  if (
    !raw ||
    typeof raw !== "object" ||
    typeof raw.event_id !== "string" ||
    typeof raw.kind !== "string" ||
    typeof raw.status !== "string" ||
    typeof raw.ts !== "string" ||
    typeof raw.priority !== "number"
  ) {
    console.warn(
      "[dispatch] workspace_event_appended: malformed envelope",
      env.data,
    );
    return;
  }
  // The backend WorkspaceEvent dataclass has no `comments` field; the
  // frontend interface adds one for the per-row thread. Default to []
  // when the envelope doesn't carry one — the comment thread refreshes
  // when the operator opens the row.
  const event: WorkspaceEvent = {
    ...(raw as WorkspaceEvent),
    comments: raw.comments ?? [],
  };
  useWorkspaceStore.getState().upsertEvent(event);
}
