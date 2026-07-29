// 2026-07-29 — transient-error expiry. A turn-scoped failure passes
// `autoClearMs` and the orb recovers to idle on its own; a persistent
// error (connectivity class) called without it must never be cleared by
// an earlier transient's stale timer.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setOrbState, TRANSIENT_ERROR_CLEAR_MS } from "./orb";
import { useEntityStore } from "../entity";

describe("setOrbState transient-error expiry", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useEntityStore.getState().setState("idle");
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    useEntityStore.getState().setState("idle");
  });

  it("auto-clears a transient error back to idle", () => {
    setOrbState("error", { autoClearMs: TRANSIENT_ERROR_CLEAR_MS });
    expect(useEntityStore.getState().state).toBe("error");

    vi.advanceTimersByTime(TRANSIENT_ERROR_CLEAR_MS + 200);
    expect(useEntityStore.getState().state).toBe("idle");
  });

  it("a persistent error is NOT cleared by an earlier transient timer", () => {
    setOrbState("error", { autoClearMs: TRANSIENT_ERROR_CLEAR_MS });
    // Connectivity-class failure lands afterwards, without auto-clear.
    setOrbState("error");

    vi.advanceTimersByTime(TRANSIENT_ERROR_CLEAR_MS * 3);
    expect(useEntityStore.getState().state).toBe("error");
  });

  it("a new turn state cancels the pending expiry", () => {
    setOrbState("error", { autoClearMs: TRANSIENT_ERROR_CLEAR_MS });
    // Lands inside the 120ms dwell window, so it queues; flush the dwell
    // timer before asserting. The expiry was cancelled at call time.
    setOrbState("thinking");
    vi.advanceTimersByTime(300);
    expect(useEntityStore.getState().state).toBe("thinking");

    vi.advanceTimersByTime(TRANSIENT_ERROR_CLEAR_MS * 3);
    expect(useEntityStore.getState().state).toBe("thinking");
  });
});
