import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const checkUpdate = vi.fn();
const applyUpdate = vi.fn();
const forceApplyUpdate = vi.fn();
const exeUpdateCheck = vi.fn();
const exeUpdateApply = vi.fn();

vi.mock("../lib/update", () => ({
  checkUpdate: (...args: unknown[]) => checkUpdate(...args),
  applyUpdate: (...args: unknown[]) => applyUpdate(...args),
  forceApplyUpdate: (...args: unknown[]) => forceApplyUpdate(...args),
  exeUpdateCheck: (...args: unknown[]) => exeUpdateCheck(...args),
  exeUpdateApply: (...args: unknown[]) => exeUpdateApply(...args),
}));

import { needsManualRestart, useUpdateStore } from "./update";

function resetStore() {
  useUpdateStore.setState({
    version: null,
    behind: 0,
    summaries: [],
    divergence: null,
    checking: false,
    applying: false,
    error: null,
    errorSource: null,
    exeAvailable: false,
    exeVersion: null,
    exeApplying: false,
  });
}

function enterTauri() {
  (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("useUpdateStore", () => {
  beforeEach(() => {
    resetStore();
    checkUpdate.mockReset();
    applyUpdate.mockReset();
    forceApplyUpdate.mockReset();
    exeUpdateCheck.mockReset();
    // Default: exe check reports nothing new; individual tests override.
    exeUpdateCheck.mockResolvedValue({
      available: false,
      version: "1.0.0",
      notes: "",
    });
    exeUpdateApply.mockReset();
    enterTauri();
  });

  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  });

  it("check() populates behind/summaries/version", async () => {
    checkUpdate.mockResolvedValue({
      behind: 3,
      summaries: ["a", "b", "c"],
      version: "abc1234",
      divergence: null,
    });
    await useUpdateStore.getState().check();
    const s = useUpdateStore.getState();
    expect(s.behind).toBe(3);
    expect(s.summaries).toEqual(["a", "b", "c"]);
    expect(s.version).toBe("abc1234");
    expect(s.checking).toBe(false);
    expect(s.error).toBeNull();
    expect(s.divergence).toBeNull();
  });

  it("check() populates divergence when the backend reports one", async () => {
    const divergence = {
      dirty: ["a.txt"],
      dirty_total: 1,
      ahead: 2,
      ahead_summaries: ["c1", "c2"],
    };
    checkUpdate.mockResolvedValue({
      behind: 1,
      summaries: ["origin commit"],
      version: "abc1234",
      divergence,
    });
    await useUpdateStore.getState().check();
    expect(useUpdateStore.getState().divergence).toEqual(divergence);
  });

  it("check() surfaces a rejected promise as a readable error, not a throw", async () => {
    checkUpdate.mockRejectedValue("network unreachable");
    await expect(useUpdateStore.getState().check()).resolves.toBeUndefined();
    const s = useUpdateStore.getState();
    expect(s.error).toBe("network unreachable");
    expect(s.errorSource).toBe("check");
    expect(s.checking).toBe(false);
  });

  it("check() is a no-op outside Tauri", async () => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    await useUpdateStore.getState().check();
    expect(checkUpdate).not.toHaveBeenCalled();
    expect(useUpdateStore.getState().checking).toBe(false);
  });

  it("apply() sets applying during the call, then refreshes state to behind: 0", async () => {
    let resolveApply!: (v: string) => void;
    applyUpdate.mockReturnValue(new Promise((r) => (resolveApply = r)));
    checkUpdate.mockResolvedValue({
      behind: 0,
      summaries: [],
      version: "def5678",
      divergence: null,
    });

    const applyPromise = useUpdateStore.getState().apply();
    expect(useUpdateStore.getState().applying).toBe(true);

    resolveApply("def5678");
    await applyPromise;

    const s = useUpdateStore.getState();
    expect(s.applying).toBe(false);
    expect(s.behind).toBe(0);
    expect(s.version).toBe("def5678");
    expect(checkUpdate).toHaveBeenCalledTimes(1);
  });

  it('holds applying: true across the whole post-apply re-check — the chip must never flash back to an enabled "update · N" state in between', async () => {
    const applyD = deferred<string>();
    const checkD = deferred<{
      behind: number;
      summaries: string[];
      version: string;
      divergence: null;
    }>();
    applyUpdate.mockReturnValue(applyD.promise);
    checkUpdate.mockReturnValue(checkD.promise);

    const applyPromise = useUpdateStore.getState().apply();
    expect(useUpdateStore.getState().applying).toBe(true);

    applyD.resolve("def5678");
    // Let applyUpdate's resolution propagate into the store's microtask
    // queue without letting the (still-pending) checkUpdate() settle.
    await Promise.resolve();
    await Promise.resolve();
    // The re-check is in flight but hasn't resolved — this is exactly the
    // window the coordinator flagged: applying must still read true here,
    // otherwise the HUD would show a stale, still-behind "update · N" pill.
    expect(useUpdateStore.getState().applying).toBe(true);

    checkD.resolve({
      behind: 0,
      summaries: [],
      version: "def5678",
      divergence: null,
    });
    await applyPromise;

    expect(useUpdateStore.getState().applying).toBe(false);
    expect(useUpdateStore.getState().behind).toBe(0);
  });

  it("a rejected concurrent apply surfaces as a readable, apply-sourced error", async () => {
    applyUpdate.mockRejectedValue("an update is already in progress");
    await expect(useUpdateStore.getState().apply()).resolves.toBeUndefined();
    const s = useUpdateStore.getState();
    expect(s.error).toBe("an update is already in progress");
    expect(s.errorSource).toBe("apply");
    expect(s.applying).toBe(false);
    // Apply failed — no need to re-check.
    expect(checkUpdate).not.toHaveBeenCalled();
  });

  it("a second apply() call while one is in flight is a client-side no-op", async () => {
    let resolveApply!: (v: string) => void;
    applyUpdate.mockReturnValue(new Promise((r) => (resolveApply = r)));
    checkUpdate.mockResolvedValue({
      behind: 0,
      summaries: [],
      version: "sha",
      divergence: null,
    });

    const first = useUpdateStore.getState().apply();
    const second = useUpdateStore.getState().apply();
    resolveApply("sha");
    await Promise.all([first, second]);

    expect(applyUpdate).toHaveBeenCalledTimes(1);
  });

  it("apply() is a no-op outside Tauri", async () => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    await useUpdateStore.getState().apply();
    expect(applyUpdate).not.toHaveBeenCalled();
    expect(useUpdateStore.getState().applying).toBe(false);
  });

  it("check() also surfaces a newer installer from the exe check", async () => {
    checkUpdate.mockResolvedValue({
      behind: 0,
      summaries: [],
      version: "1.0.4 (abc1234)",
      divergence: null,
    });
    exeUpdateCheck.mockResolvedValue({
      available: true,
      version: "1.0.5",
      notes: "notes",
    });
    await useUpdateStore.getState().check();
    const s = useUpdateStore.getState();
    expect(s.exeAvailable).toBe(true);
    expect(s.exeVersion).toBe("1.0.5");
  });

  it("a failed exe check keeps the git-check results and stays silent", async () => {
    checkUpdate.mockResolvedValue({
      behind: 2,
      summaries: ["a", "b"],
      version: "1.0.4 (abc1234)",
      divergence: null,
    });
    exeUpdateCheck.mockRejectedValue("network sadness");
    await useUpdateStore.getState().check();
    const s = useUpdateStore.getState();
    expect(s.behind).toBe(2);
    expect(s.exeAvailable).toBe(false);
    expect(s.error).toBeNull();
  });

  it("exeApply() reports a failed handoff and clears exeApplying", async () => {
    exeUpdateApply.mockRejectedValue("installer hash mismatch");
    await useUpdateStore.getState().exeApply();
    const s = useUpdateStore.getState();
    expect(s.exeApplying).toBe(false);
    expect(s.error).toBe("installer hash mismatch");
    expect(s.errorSource).toBe("apply");
  });

  it("forceApply() sets applying during the call, then re-checks", async () => {
    let resolveForceApply!: (v: string) => void;
    forceApplyUpdate.mockReturnValue(
      new Promise((r) => (resolveForceApply = r)),
    );
    checkUpdate.mockResolvedValue({
      behind: 0,
      summaries: [],
      version: "def5678",
      divergence: null,
    });

    const forceApplyPromise = useUpdateStore.getState().forceApply();
    expect(useUpdateStore.getState().applying).toBe(true);

    resolveForceApply("def5678");
    await forceApplyPromise;

    const s = useUpdateStore.getState();
    expect(s.applying).toBe(false);
    expect(s.version).toBe("def5678");
    expect(s.divergence).toBeNull();
    expect(forceApplyUpdate).toHaveBeenCalledTimes(1);
    expect(checkUpdate).toHaveBeenCalledTimes(1);
  });

  it("a rejected forceApply() surfaces as a readable, apply-sourced error without re-checking", async () => {
    forceApplyUpdate.mockRejectedValue("reset failed: origin/main unresolved");
    await expect(
      useUpdateStore.getState().forceApply(),
    ).resolves.toBeUndefined();
    const s = useUpdateStore.getState();
    expect(s.error).toBe("reset failed: origin/main unresolved");
    expect(s.errorSource).toBe("apply");
    expect(s.applying).toBe(false);
    expect(checkUpdate).not.toHaveBeenCalled();
  });

  it("forceApply() is a no-op outside Tauri", async () => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    await useUpdateStore.getState().forceApply();
    expect(forceApplyUpdate).not.toHaveBeenCalled();
    expect(useUpdateStore.getState().applying).toBe(false);
  });

  it("forceApply() while apply() is already in flight is a client-side no-op, sharing the same applying guard", async () => {
    let resolveApply!: (v: string) => void;
    applyUpdate.mockReturnValue(new Promise((r) => (resolveApply = r)));
    checkUpdate.mockResolvedValue({
      behind: 0,
      summaries: [],
      version: "sha",
      divergence: null,
    });

    const applyPromise = useUpdateStore.getState().apply();
    const forceApplyPromise = useUpdateStore.getState().forceApply();
    resolveApply("sha");
    await Promise.all([applyPromise, forceApplyPromise]);

    expect(applyUpdate).toHaveBeenCalledTimes(1);
    expect(forceApplyUpdate).not.toHaveBeenCalled();
  });

  it("needsManualRestart distinguishes the dead-app phrase from an ordinary retryable failure", () => {
    expect(
      needsManualRestart(
        "update failed (fast-forward error); additionally failed to restart the app — restart TESSERACT manually",
      ),
    ).toBe(true);
    expect(
      needsManualRestart(
        "updated to abc123, but restarting the app failed (spawn error) — restart TESSERACT manually",
      ),
    ).toBe(true);
    expect(needsManualRestart("an update is already in progress")).toBe(false);
    expect(
      needsManualRestart(
        "update failed (network error); restarted on the previous version",
      ),
    ).toBe(false);
  });
});
