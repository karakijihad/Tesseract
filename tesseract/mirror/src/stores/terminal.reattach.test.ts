/**
 * F6 (terminal daily-driver, 2026-07-05) — client-side reattach handshake.
 *
 * `bootstrapPanes()` (page-reload rehydrate) now asks to reattach every
 * persisted leaf's pty instead of unconditionally respawning.
 * `terminal_reattached` marks the pane running again in place;
 * `terminal_reattach_failed` falls back to the pre-F6 respawn (`sendStart`).
 *
 * Live-gate fix pass (2026-07-05) — two spec gaps found in a live exit-gate:
 *
 * Finding 1: `bootstrapPanes()` runs exactly when every xterm is brand new
 * (page bootstrap/reload). It now always asks for a FULL replay
 * (`fresh: true`, no since_token), ignoring the persisted `lastSeenCursor`
 * from a previous page lifetime — sending that stale cursor produced a
 * gap-only replay into a blank terminal, i.e. a blank pane after reload.
 *
 * Finding 2: a WS reconnect (e.g. backend restart) never re-ran the pane
 * handshake — only the one-shot page-mount bootstrap did. `terminal.ts`
 * now exposes `reattachAfterReconnect()` (called from `websocket.ts` on
 * every reconnect after the first connect) which asks for a gap-only
 * replay per pane (the xterm still has content — the page never reloaded)
 * and falls back to respawn via the existing `terminal_reattach_failed`
 * path when the backend reports the pane gone.
 *
 * Live re-verification finding (same session) — the full replay from
 * Finding 1 exposed a THIRD bug: replayed bytes can contain terminal
 * query sequences the shell/ConPTY emitted live (device attributes
 * ESC[c, DSR ESC[6n). xterm.js auto-answers those via `onData` while
 * re-parsing them on replay, and that synthetic answer used to flow
 * straight into `sendKeystroke` → the shell's stdin, corrupting the
 * operator's first typed command after a reload
 * (`^[[?1;2cecho %P5B%` observed live). The backend now flags a replay
 * chunk (`terminal_output_chunk.replay: true`); the client marks the
 * pane "replaying" for the duration of that `term.write` and
 * `sendKeystroke` drops onData emissions while it's set.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useTerminalStore } from './terminal';
import type { PaneLeaf, TerminalTab } from '../lib/types';
import type { Terminal } from '@xterm/xterm';

Object.defineProperty(globalThis, 'localStorage', {
  value: {
    getItem: () => null,
    setItem: () => undefined,
    removeItem: () => undefined,
  },
  writable: true,
});

const sendRawMock = vi.fn();
vi.mock('./websocket', () => ({
  useWebSocketStore: { getState: () => ({ sendRaw: sendRawMock }) },
}));

function leaf(id: string, overrides: Partial<PaneLeaf> = {}): PaneLeaf {
  return {
    type: 'leaf',
    id,
    ptyStatus: 'stopped',
    label: 'cmd',
    shell: 'cmd',
    errorMessage: null,
    owner: 'user',
    observerEnabled: true,
    lastSeenCursor: 0,
    ...overrides,
  };
}

function tab(id: string, root: PaneLeaf): TerminalTab {
  return { id, label: root.label, root };
}

function resetStore() {
  useTerminalStore.setState({
    tabs: [],
    activeTabId: null,
    focusedPaneId: null,
    _terms: new Map(),
    _watermarks: new Map(),
    _pausedPanes: new Set(),
    _replayingPanes: new Set(),
  });
  sendRawMock.mockClear();
}

describe('terminal reattach handshake (F6)', () => {
  beforeEach(resetStore);

  it('bootstrapPanes sends terminal_reattach (not terminal_start), requesting a FULL replay regardless of the persisted cursor', () => {
    // Live-gate Finding 1 — a non-zero persisted cursor from a previous
    // page lifetime must NOT turn this into a gap-only replay: the xterm
    // this pane is about to attach to has zero content.
    const persistedLeaf = leaf('pane-persisted', { lastSeenCursor: 42 });
    useTerminalStore.setState({
      tabs: [tab('tab-1', persistedLeaf)],
      activeTabId: 'tab-1',
    });

    useTerminalStore.getState().bootstrapPanes();

    const reattachCalls = sendRawMock.mock.calls.filter(
      ([m]) => (m as { type: string }).type === 'terminal_reattach',
    );
    expect(reattachCalls).toHaveLength(1);
    expect(reattachCalls[0][0]).toMatchObject({
      type: 'terminal_reattach',
      pane_id: 'pane-persisted',
      since_token: null,
      fresh: true,
    });
    const startCalls = sendRawMock.mock.calls.filter(
      ([m]) => (m as { type: string }).type === 'terminal_start',
    );
    expect(startCalls).toHaveLength(0);
  });

  it('reattachAfterReconnect sends terminal_reattach with the live cursor and fresh: false (gap replay)', () => {
    // Live-gate Finding 2 — a WS reconnect (not a page reload) leaves the
    // xterm's content intact, so only the gap since the last-seen cursor
    // should be requested — a full replay here would double-echo
    // everything already on screen.
    const persistedLeaf = leaf('pane-live', { lastSeenCursor: 42, ptyStatus: 'running' });
    useTerminalStore.setState({
      tabs: [tab('tab-1', persistedLeaf)],
      activeTabId: 'tab-1',
    });

    useTerminalStore.getState().reattachAfterReconnect();

    const reattachCalls = sendRawMock.mock.calls.filter(
      ([m]) => (m as { type: string }).type === 'terminal_reattach',
    );
    expect(reattachCalls).toHaveLength(1);
    expect(reattachCalls[0][0]).toMatchObject({
      type: 'terminal_reattach',
      pane_id: 'pane-live',
      since_token: '42',
      fresh: false,
    });
  });

  it('reattachAfterReconnect resets outstanding client flow-control state for the pane', () => {
    const persistedLeaf = leaf('pane-paused', { lastSeenCursor: 5, ptyStatus: 'running' });
    useTerminalStore.setState({
      tabs: [tab('tab-1', persistedLeaf)],
      activeTabId: 'tab-1',
      _watermarks: new Map([['pane-paused', 150_000]]),
      _pausedPanes: new Set(['pane-paused']),
    });

    useTerminalStore.getState().reattachAfterReconnect();

    expect(useTerminalStore.getState()._watermarks.has('pane-paused')).toBe(false);
    expect(useTerminalStore.getState()._pausedPanes.has('pane-paused')).toBe(false);
    const resumeCalls = sendRawMock.mock.calls.filter(
      ([m]) => (m as { type: string }).type === 'terminal_resume',
    );
    expect(resumeCalls).toHaveLength(1);
    expect(resumeCalls[0][0]).toMatchObject({ type: 'terminal_resume', pane_id: 'pane-paused' });
  });

  it('suppresses sendKeystroke for a query-response that fires synchronously while writing a replay chunk', () => {
    // Simulates the real xterm.js behavior: parsing a device-attributes
    // query (ESC[c) during `term.write` triggers an immediate synchronous
    // `onData` callback with the query's answer — BEFORE the write's own
    // completion callback fires. In the app this onData handler calls
    // `sendKeystroke`; here we call it directly since there's no mounted
    // TerminalInstance in this test.
    let completeWrite: (() => void) | null = null;
    const fakeTerm = {
      write: (_bytes: string, cb?: () => void) => {
        useTerminalStore.getState().sendKeystroke('pane-1', '\x1b[?1;2c');
        completeWrite = cb ?? null;
      },
    } as unknown as Terminal;
    useTerminalStore.setState({
      tabs: [tab('tab-1', leaf('pane-1'))],
      activeTabId: 'tab-1',
      _terms: new Map([['pane-1', fakeTerm]]),
    });

    useTerminalStore.getState().handleRawMessage({
      type: 'terminal_output_chunk',
      data: { pane_id: 'pane-1', bytes: 'prompt>\x1b[?1;2c', replay: true },
    });

    const keystrokeCallsDuringReplay = sendRawMock.mock.calls.filter(
      ([m]) => (m as { type: string }).type === 'terminal_keystroke',
    );
    expect(keystrokeCallsDuringReplay).toHaveLength(0);
    expect(useTerminalStore.getState()._replayingPanes.has('pane-1')).toBe(true);

    // Write completes — the replay guard lifts and real typing resumes.
    completeWrite!();
    expect(useTerminalStore.getState()._replayingPanes.has('pane-1')).toBe(false);

    useTerminalStore.getState().sendKeystroke('pane-1', 'echo hi\r');
    const keystrokeCallsAfter = sendRawMock.mock.calls.filter(
      ([m]) => (m as { type: string }).type === 'terminal_keystroke',
    );
    expect(keystrokeCallsAfter).toHaveLength(1);
    expect(keystrokeCallsAfter[0][0]).toMatchObject({ pane_id: 'pane-1', bytes: 'echo hi\r' });
  });

  it('does NOT suppress sendKeystroke for a live (non-replay) output chunk', () => {
    let completeWrite: (() => void) | null = null;
    const fakeTerm = {
      write: (_bytes: string, cb?: () => void) => {
        completeWrite = cb ?? null;
      },
    } as unknown as Terminal;
    useTerminalStore.setState({
      tabs: [tab('tab-1', leaf('pane-1'))],
      activeTabId: 'tab-1',
      _terms: new Map([['pane-1', fakeTerm]]),
    });

    useTerminalStore.getState().handleRawMessage({
      type: 'terminal_output_chunk',
      data: { pane_id: 'pane-1', bytes: 'ready>' },
    });
    expect(useTerminalStore.getState()._replayingPanes.has('pane-1')).toBe(false);

    useTerminalStore.getState().sendKeystroke('pane-1', 'a');
    const keystrokeCalls = sendRawMock.mock.calls.filter(
      ([m]) => (m as { type: string }).type === 'terminal_keystroke',
    );
    expect(keystrokeCalls).toHaveLength(1);
    completeWrite!();
  });

  it('terminal_reattached flips the pane to running without a respawn', () => {
    const persistedLeaf = leaf('pane-persisted', { ptyStatus: 'starting' });
    useTerminalStore.setState({
      tabs: [tab('tab-1', persistedLeaf)],
      activeTabId: 'tab-1',
    });

    useTerminalStore.getState().handleRawMessage({
      type: 'terminal_reattached',
      data: { pane_id: 'pane-persisted', backend: 'winpty', observer_enabled: true },
    });

    const found = useTerminalStore.getState().tabs[0].root as PaneLeaf;
    expect(found.ptyStatus).toBe('running');
    const startCalls = sendRawMock.mock.calls.filter(
      ([m]) => (m as { type: string }).type === 'terminal_start',
    );
    expect(startCalls).toHaveLength(0);
  });

  it('terminal_reattach_failed falls back to sendStart with the leaf\'s shell', () => {
    const persistedLeaf = leaf('pane-gone', { shell: 'bash', ptyStatus: 'starting' });
    useTerminalStore.setState({
      tabs: [tab('tab-1', persistedLeaf)],
      activeTabId: 'tab-1',
    });

    useTerminalStore.getState().handleRawMessage({
      type: 'terminal_reattach_failed',
      data: { pane_id: 'pane-gone' },
    });

    const startCalls = sendRawMock.mock.calls.filter(
      ([m]) => (m as { type: string }).type === 'terminal_start',
    );
    expect(startCalls).toHaveLength(1);
    expect(startCalls[0][0]).toMatchObject({ pane_id: 'pane-gone', shell: 'bash' });
  });

  it('advances lastSeenCursor by exactly the received byte count', () => {
    const persistedLeaf = leaf('pane-1', { lastSeenCursor: 10 });
    useTerminalStore.setState({
      tabs: [tab('tab-1', persistedLeaf)],
      activeTabId: 'tab-1',
      _terms: new Map(), // no attached term — write is skipped, cursor still advances
    });

    useTerminalStore.getState().handleRawMessage({
      type: 'terminal_output_chunk',
      data: { pane_id: 'pane-1', bytes: 'hello' },
    });

    const found = useTerminalStore.getState().tabs[0].root as PaneLeaf;
    expect(found.lastSeenCursor).toBe(15);
  });
});
