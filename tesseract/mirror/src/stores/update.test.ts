import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const checkUpdate = vi.fn();
const applyUpdate = vi.fn();

vi.mock('../lib/update', () => ({
  checkUpdate: (...args: unknown[]) => checkUpdate(...args),
  applyUpdate: (...args: unknown[]) => applyUpdate(...args),
}));

import { useUpdateStore } from './update';

function resetStore() {
  useUpdateStore.setState({
    version: null,
    behind: 0,
    summaries: [],
    checking: false,
    applying: false,
    error: null,
  });
}

function enterTauri() {
  (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
}

describe('useUpdateStore', () => {
  beforeEach(() => {
    resetStore();
    checkUpdate.mockReset();
    applyUpdate.mockReset();
    enterTauri();
  });

  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  });

  it('check() populates behind/summaries/version', async () => {
    checkUpdate.mockResolvedValue({ behind: 3, summaries: ['a', 'b', 'c'], version: 'abc1234' });
    await useUpdateStore.getState().check();
    const s = useUpdateStore.getState();
    expect(s.behind).toBe(3);
    expect(s.summaries).toEqual(['a', 'b', 'c']);
    expect(s.version).toBe('abc1234');
    expect(s.checking).toBe(false);
    expect(s.error).toBeNull();
  });

  it('check() surfaces a rejected promise as a readable error, not a throw', async () => {
    checkUpdate.mockRejectedValue('network unreachable');
    await expect(useUpdateStore.getState().check()).resolves.toBeUndefined();
    expect(useUpdateStore.getState().error).toBe('network unreachable');
    expect(useUpdateStore.getState().checking).toBe(false);
  });

  it('check() is a no-op outside Tauri', async () => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    await useUpdateStore.getState().check();
    expect(checkUpdate).not.toHaveBeenCalled();
    expect(useUpdateStore.getState().checking).toBe(false);
  });

  it('apply() sets applying during the call, then refreshes state to behind: 0', async () => {
    let resolveApply!: (v: string) => void;
    applyUpdate.mockReturnValue(new Promise((r) => (resolveApply = r)));
    checkUpdate.mockResolvedValue({ behind: 0, summaries: [], version: 'def5678' });

    const applyPromise = useUpdateStore.getState().apply();
    expect(useUpdateStore.getState().applying).toBe(true);

    resolveApply('def5678');
    await applyPromise;

    const s = useUpdateStore.getState();
    expect(s.applying).toBe(false);
    expect(s.behind).toBe(0);
    expect(s.version).toBe('def5678');
    expect(checkUpdate).toHaveBeenCalledTimes(1);
  });

  it('a rejected concurrent apply surfaces as a readable error', async () => {
    applyUpdate.mockRejectedValue('an update is already in progress');
    await expect(useUpdateStore.getState().apply()).resolves.toBeUndefined();
    const s = useUpdateStore.getState();
    expect(s.error).toBe('an update is already in progress');
    expect(s.applying).toBe(false);
    // Apply failed — no need to re-check.
    expect(checkUpdate).not.toHaveBeenCalled();
  });

  it('a second apply() call while one is in flight is a client-side no-op', async () => {
    let resolveApply!: (v: string) => void;
    applyUpdate.mockReturnValue(new Promise((r) => (resolveApply = r)));
    checkUpdate.mockResolvedValue({ behind: 0, summaries: [], version: 'sha' });

    const first = useUpdateStore.getState().apply();
    const second = useUpdateStore.getState().apply();
    resolveApply('sha');
    await Promise.all([first, second]);

    expect(applyUpdate).toHaveBeenCalledTimes(1);
  });

  it('apply() is a no-op outside Tauri', async () => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    await useUpdateStore.getState().apply();
    expect(applyUpdate).not.toHaveBeenCalled();
    expect(useUpdateStore.getState().applying).toBe(false);
  });
});
