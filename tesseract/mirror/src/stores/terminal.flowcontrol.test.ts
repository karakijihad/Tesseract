/**
 * F2 (terminal daily-driver, 2026-07-05) — client-side output flow control.
 *
 * The client tracks a byte watermark per pane on `terminal_output_chunk`.
 * Crossing HIGH sends `terminal_pause` (once); the `term.write` callback
 * draining below LOW sends `terminal_resume` (once). Mirrors the server
 * pause/resume tests in `tesseract/tests/fix_pass_terminal_control_2026_05_16/
 * test_pty_flow_control.py`.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useTerminalStore, WATERMARK_HIGH } from './terminal';
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

function resetStore() {
  useTerminalStore.setState({
    tabs: [
      {
        id: 'tab-1',
        label: 'cmd',
        root: {
          type: 'leaf',
          id: 'pane-1',
          ptyStatus: 'running',
          label: 'cmd',
          shell: 'cmd',
          errorMessage: null,
          owner: 'user',
          observerEnabled: true,
          lastSeenCursor: 0,
        },
      },
    ],
    activeTabId: 'tab-1',
    focusedPaneId: 'pane-1',
    _terms: new Map(),
    _watermarks: new Map(),
    _pausedPanes: new Set(),
    _replayingPanes: new Set(),
  });
  sendRawMock.mockClear();
}

function pauseCalls() {
  return sendRawMock.mock.calls.filter(([m]) => (m as { type: string }).type === 'terminal_pause');
}

function resumeCalls() {
  return sendRawMock.mock.calls.filter(([m]) => (m as { type: string }).type === 'terminal_resume');
}

describe('terminal output flow control (F2)', () => {
  beforeEach(resetStore);

  it('sends terminal_pause exactly once when the watermark crosses HIGH', () => {
    const pending: Array<() => void> = [];
    const fakeTerm = {
      write: (_bytes: string, cb?: () => void) => {
        if (cb) pending.push(cb); // never drained in this test — buffered
      },
    } as unknown as Terminal;
    useTerminalStore.setState({ _terms: new Map([['pane-1', fakeTerm]]) });

    const bigChunk = 'x'.repeat(WATERMARK_HIGH + 1);
    useTerminalStore.getState().handleRawMessage({
      type: 'terminal_output_chunk',
      data: { pane_id: 'pane-1', bytes: bigChunk },
    });
    expect(pauseCalls()).toHaveLength(1);
    expect(pauseCalls()[0][0]).toMatchObject({ type: 'terminal_pause', pane_id: 'pane-1' });

    // A second oversized chunk while still paused must NOT re-send pause.
    useTerminalStore.getState().handleRawMessage({
      type: 'terminal_output_chunk',
      data: { pane_id: 'pane-1', bytes: bigChunk },
    });
    expect(pauseCalls()).toHaveLength(1);
  });

  it('sends terminal_resume exactly once when the watermark drains below LOW', () => {
    const pending: Array<() => void> = [];
    const fakeTerm = {
      write: (_bytes: string, cb?: () => void) => {
        if (cb) pending.push(cb);
      },
    } as unknown as Terminal;
    useTerminalStore.setState({ _terms: new Map([['pane-1', fakeTerm]]) });

    const bigChunk = 'x'.repeat(WATERMARK_HIGH + 1);
    useTerminalStore.getState().handleRawMessage({
      type: 'terminal_output_chunk',
      data: { pane_id: 'pane-1', bytes: bigChunk },
    });
    expect(pauseCalls()).toHaveLength(1);
    expect(resumeCalls()).toHaveLength(0);

    // Simulate xterm finishing the parse — fires the write callback,
    // draining the watermark back to 0 (well under LOW).
    pending.forEach((cb) => cb());

    expect(resumeCalls()).toHaveLength(1);
    expect(resumeCalls()[0][0]).toMatchObject({ type: 'terminal_resume', pane_id: 'pane-1' });

    // Draining again (idempotent) must not re-send resume.
    pending.forEach((cb) => cb());
    expect(resumeCalls()).toHaveLength(1);
  });

  it('does not pause for a small chunk under HIGH', () => {
    const fakeTerm = {
      write: (_bytes: string, cb?: () => void) => cb?.(),
    } as unknown as Terminal;
    useTerminalStore.setState({ _terms: new Map([['pane-1', fakeTerm]]) });

    useTerminalStore.getState().handleRawMessage({
      type: 'terminal_output_chunk',
      data: { pane_id: 'pane-1', bytes: 'small chunk' },
    });
    expect(pauseCalls()).toHaveLength(0);
  });

  it('resets pause state and sends a deadlock-guard resume on detachTerminal', () => {
    const pending: Array<() => void> = [];
    const fakeTerm = {
      write: (_bytes: string, cb?: () => void) => {
        if (cb) pending.push(cb);
      },
    } as unknown as Terminal;
    useTerminalStore.setState({ _terms: new Map([['pane-1', fakeTerm]]) });

    const bigChunk = 'x'.repeat(WATERMARK_HIGH + 1);
    useTerminalStore.getState().handleRawMessage({
      type: 'terminal_output_chunk',
      data: { pane_id: 'pane-1', bytes: bigChunk },
    });
    expect(pauseCalls()).toHaveLength(1);
    expect(resumeCalls()).toHaveLength(0);

    // Term disposed/reset while paused (pending write callback never fires).
    useTerminalStore.getState().detachTerminal('pane-1');

    expect(resumeCalls()).toHaveLength(1);
  });
});
