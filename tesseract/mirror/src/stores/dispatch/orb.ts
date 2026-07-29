import type { EntityState } from "../../lib/types";
import { useEntityStore } from "../entity";

// Min-dwell debounce on orb state flips. WebSocket envelopes can land in
// the same JS task (e.g. `loop_start` → `stream_tool_call_start` →
// `stream_text` → `loop_end` on a one-shot tool turn) and each fires a
// synchronous `entity.setState`. Without dwell, intermediate states
// like `thinking` get overwritten before any frame ever renders them
// — the orb appears to skip directly from idle to speaking. The
// EntityController's per-frame lerp (`_currentPulseHz` etc.) only
// helps if the target value is held long enough to be seen; this dwell
// guarantees that.
//
// Contract:
//  - First flip after a quiet period applies immediately and arms the lock.
//  - Further flips within DWELL_MS queue the latest target. When the
//    lock expires, the most recent queued state is applied.
//  - `'error'` bypasses dwell and clears any pending flip — a real
//    failure must reach the operator without any 120ms blanket window.
const _ORB_DWELL_MS = 120;
let _orbDwellLockUntil = 0;
let _orbPendingState: EntityState | null = null;
let _orbPendingTimer: ReturnType<typeof setTimeout> | null = null;

// Transient-error expiry (2026-07-29). A failed chat turn or command used
// to leave the orb red until the operator's next interaction — nothing
// time-based ever cleared it. Turn-scoped failures now pass
// `autoClearMs` and revert to idle on expiry; connectivity-driven errors
// (websocket.ts after max reconnects — the "TESSERACT is actually
// broken" class) call without it and persist. Any later setOrbState call
// cancels a pending expiry, so a persistent error can never be cleared
// by an earlier transient's stale timer.
let _orbErrorExpiryTimer: ReturnType<typeof setTimeout> | null = null;

// How long a turn-scoped error holds the orb before it recovers to idle.
export const TRANSIENT_ERROR_CLEAR_MS = 5000;

function _cancelErrorExpiry(): void {
  if (_orbErrorExpiryTimer !== null) {
    clearTimeout(_orbErrorExpiryTimer);
    _orbErrorExpiryTimer = null;
  }
}

export function setOrbState(
  target: EntityState,
  opts?: { autoClearMs?: number },
): void {
  const now = performance.now();
  const apply = (next: EntityState): void => {
    const fresh = useEntityStore.getState();
    if (fresh.state !== next) fresh.setState(next);
  };

  _cancelErrorExpiry();

  if (target === "error") {
    if (_orbPendingTimer !== null) {
      clearTimeout(_orbPendingTimer);
      _orbPendingTimer = null;
      _orbPendingState = null;
    }
    apply(target);
    _orbDwellLockUntil = now + _ORB_DWELL_MS;
    const autoClearMs = opts?.autoClearMs;
    if (autoClearMs !== undefined) {
      _orbErrorExpiryTimer = setTimeout(() => {
        _orbErrorExpiryTimer = null;
        // Only clear our own error: anything else that ran meanwhile
        // (a new turn, a persistent error) already cancelled this timer.
        if (useEntityStore.getState().state === "error") {
          setOrbState("idle");
        }
      }, autoClearMs);
    }
    return;
  }

  if (now >= _orbDwellLockUntil) {
    apply(target);
    _orbDwellLockUntil = now + _ORB_DWELL_MS;
    if (_orbPendingTimer !== null) {
      clearTimeout(_orbPendingTimer);
      _orbPendingTimer = null;
    }
    _orbPendingState = null;
    return;
  }

  _orbPendingState = target;
  if (_orbPendingTimer !== null) return;
  const remainingMs = Math.max(0, _orbDwellLockUntil - now);
  _orbPendingTimer = setTimeout(() => {
    _orbPendingTimer = null;
    const final = _orbPendingState;
    _orbPendingState = null;
    if (final !== null) apply(final);
    _orbDwellLockUntil = performance.now() + _ORB_DWELL_MS;
  }, remainingMs);
}

// Server-authoritative orb writes (e.g. `entity_state_set`) bypass
// `setOrbState`'s dwell gating entirely — the backend already decided
// the state, so flush any queued dwell flip first (a late-firing timer
// must not clobber it) and re-arm the dwell window from now.
export function resetOrbDwellForServerWrite(): void {
  if (_orbPendingTimer !== null) {
    clearTimeout(_orbPendingTimer);
    _orbPendingTimer = null;
    _orbPendingState = null;
  }
  _orbDwellLockUntil = performance.now() + _ORB_DWELL_MS;
}
