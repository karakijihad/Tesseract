import { getController } from "../../lib/entity/registry";

export type Signals = NonNullable<
  ReturnType<NonNullable<ReturnType<typeof getController>>["getSignals"]>
>;

// rAF-gated text-delta impulse to the orb intensity model. Without this,
// every backend `stream_text` envelope (often 1-5 chars) hit
// `signals.onTextDelta` directly, making the orb's intensity jitter at
// chunk cadence even though the chat-bubble path was already buffered.
// Same pattern as `_scheduleFlush` in conversation.ts: sum char counts
// across the frame, fire one impulse, reset. Per-tab module state.
let _pendingTextChars = 0;
let _signalsRafHandle: number | null = null;
export function scheduleSignalsImpulse(signals: Signals): void {
  if (_signalsRafHandle !== null) return;
  const raf =
    typeof requestAnimationFrame === "function"
      ? requestAnimationFrame
      : (cb: FrameRequestCallback) =>
          setTimeout(() => cb(performance.now()), 16) as unknown as number;
  _signalsRafHandle = raf(() => {
    _signalsRafHandle = null;
    if (_pendingTextChars === 0) return;
    const chars = _pendingTextChars;
    _pendingTextChars = 0;
    signals.onTextDelta(chars);
  });
}

export function addPendingTextChars(count: number): void {
  _pendingTextChars += count;
}
