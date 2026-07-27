// Q3 frontend — shared by ChatInput/HudChatInput's "redirect now" button.
// Distinct from queue (default Enter/Send while streaming, chatQueue.ts) and
// cancel (Stop button, always available while streaming): steer only makes
// sense once there's actually a turn to redirect AND text to redirect it
// with.

/** Whether the "redirect now" (steer) button should be visible/enabled:
 * a turn must be actively streaming and the draft must have non-whitespace
 * content to send. */
export function canSteer(isStreaming: boolean, draft: string): boolean {
  return isStreaming && draft.trim().length > 0;
}
