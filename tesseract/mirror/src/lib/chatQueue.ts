import type { ChatMessage } from './types';

// Q2 frontend — shared by ChatInput/HudChatInput's composer-level queue
// chip. FIFO queue slots are per-chat and 1-based (see
// stores/conversation.ts::_nextQueuePosition); the tail of the currently
// queued messages is whatever the operator most recently sent.

/** FIFO position (1-based) of the most recently queued message, or null
 * if nothing is currently queued. */
export function lastQueuedPosition(messages: ChatMessage[]): number | null {
  const queued = messages.filter((m) => m.status === 'queued');
  return queued.length > 0 ? (queued[queued.length - 1].queuePosition ?? null) : null;
}

/** Composer chip text for a FIFO position, or null when no chip should
 * render. Position 1 (next up) is never worth flagging. */
export function queueChipLabel(position: number | null): string | null {
  if (position === null || position <= 1) return null;
  return `queued · ${position - 1} ahead`;
}
