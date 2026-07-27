/**
 * B6 — terminal store: controllerSessions slice + agent_spawned regression guard.
 *
 * Test 1: fetchControllerSessions populates the store and preserves operator_facing flags.
 * Test 2: terminal_started with agent_spawned:true auto-creates a tab (regression guard).
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useTerminalStore } from './terminal';

// ── Dependency stubs ────────────────────────────────────────────────────────

// Stub out the zustand-persist localStorage target so jsdom doesn't throw.
Object.defineProperty(globalThis, 'localStorage', {
  value: {
    getItem: () => null,
    setItem: () => undefined,
    removeItem: () => undefined,
  },
  writable: true,
});

// The terminal store sends WS messages via useWebSocketStore.getState().sendRaw.
// Stub the module so those calls are no-ops.
vi.mock('./websocket', () => ({
  useWebSocketStore: { getState: () => ({ sendRaw: () => undefined }) },
}));

// ── Helpers ─────────────────────────────────────────────────────────────────

function resetStore() {
  // Reset to initial state between tests via Zustand's setState.
  useTerminalStore.setState({
    tabs: [],
    activeTabId: null,
    config: null,
    controllerSessions: [],
    focusedPaneId: null,
  });
}

// ── Test 1: fetchControllerSessions ─────────────────────────────────────────

describe('fetchControllerSessions', () => {
  beforeEach(() => {
    resetStore();
  });

  it('stores both sessions and preserves operator_facing flags', async () => {
    const mockSessions = [
      {
        session_id: 'sess-mirror-01',
        origin: 'mirror',
        status: 'active',
        title: 'Operator terminal',
        last_active_at: '2026-05-25T10:00:00Z',
        operator_facing: true,
      },
      {
        session_id: 'sess-auto-02',
        origin: 'autonomy',
        status: 'idle',
        title: 'Background worker',
        last_active_at: '2026-05-25T09:00:00Z',
        operator_facing: false,
      },
    ];

    globalThis.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      json: async () => ({ sessions: mockSessions }),
    } as Response);

    await useTerminalStore.getState().fetchControllerSessions();

    const { controllerSessions } = useTerminalStore.getState();
    expect(controllerSessions).toHaveLength(2);

    const mirrorSession = controllerSessions.find((s) => s.session_id === 'sess-mirror-01');
    expect(mirrorSession).toBeDefined();
    expect(mirrorSession!.operator_facing).toBe(true);
    expect(mirrorSession!.origin).toBe('mirror');

    const autoSession = controllerSessions.find((s) => s.session_id === 'sess-auto-02');
    expect(autoSession).toBeDefined();
    expect(autoSession!.operator_facing).toBe(false);
    expect(autoSession!.origin).toBe('autonomy');
  });

  it('leaves existing list intact when the fetch fails', async () => {
    const existing = [
      {
        session_id: 'sess-prior',
        origin: 'mirror',
        status: 'active',
        title: 'Prior session',
        last_active_at: '2026-05-25T08:00:00Z',
        operator_facing: true,
      },
    ];
    useTerminalStore.setState({ controllerSessions: existing });

    globalThis.fetch = vi.fn().mockRejectedValueOnce(new TypeError('Failed to fetch'));

    await useTerminalStore.getState().fetchControllerSessions();

    // List must not be cleared on failure.
    expect(useTerminalStore.getState().controllerSessions).toHaveLength(1);
    expect(useTerminalStore.getState().controllerSessions[0].session_id).toBe('sess-prior');
  });

  it('exposes operator_facing flag for filtering (selector-style usage)', async () => {
    const mockSessions = [
      {
        session_id: 'sess-op',
        origin: 'mirror',
        status: 'active',
        title: 'Op session',
        last_active_at: '2026-05-25T10:00:00Z',
        operator_facing: true,
      },
      {
        session_id: 'sess-bg',
        origin: 'autonomy',
        status: 'idle',
        title: 'Background session',
        last_active_at: '2026-05-25T09:00:00Z',
        operator_facing: false,
      },
    ];

    globalThis.fetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      json: async () => ({ sessions: mockSessions }),
    } as Response);

    await useTerminalStore.getState().fetchControllerSessions();

    const { controllerSessions } = useTerminalStore.getState();
    const operatorFacing = controllerSessions.filter((s) => s.operator_facing);
    const background = controllerSessions.filter((s) => !s.operator_facing);

    expect(operatorFacing).toHaveLength(1);
    expect(operatorFacing[0].session_id).toBe('sess-op');
    expect(background).toHaveLength(1);
    expect(background[0].session_id).toBe('sess-bg');
  });
});

// ── Test 2: agent_spawned auto-tab regression guard ─────────────────────────

describe('handleRawMessage terminal_started with agent_spawned:true', () => {
  beforeEach(() => {
    resetStore();
  });

  it('auto-creates a tab when agent_spawned is true and pane does not exist', () => {
    expect(useTerminalStore.getState().tabs).toHaveLength(0);

    useTerminalStore.getState().handleRawMessage({
      type: 'terminal_started',
      data: {
        pane_id: 'pty_abc123',
        backend: 'winpty',
        agent_spawned: true,
        name: 'claude',
        observer_enabled: false,
      },
    });

    const { tabs } = useTerminalStore.getState();
    expect(tabs).toHaveLength(1);
    expect(tabs[0].root.type).toBe('leaf');
    expect(tabs[0].root.id).toBe('pty_abc123');
  });

  it('does NOT auto-create a second tab when the pane already exists', () => {
    // First message creates the tab.
    useTerminalStore.getState().handleRawMessage({
      type: 'terminal_started',
      data: {
        pane_id: 'pty_abc123',
        backend: 'winpty',
        agent_spawned: true,
        name: 'claude',
      },
    });
    expect(useTerminalStore.getState().tabs).toHaveLength(1);

    // Second message for the same pane_id must be idempotent.
    useTerminalStore.getState().handleRawMessage({
      type: 'terminal_started',
      data: {
        pane_id: 'pty_abc123',
        backend: 'winpty',
        agent_spawned: true,
        name: 'claude',
      },
    });
    expect(useTerminalStore.getState().tabs).toHaveLength(1);
  });

  it('does NOT steal operator focus when a tab already exists', () => {
    // Simulate operator having an existing tab.
    useTerminalStore.setState({
      tabs: [
        {
          id: 'tab-operator',
          label: 'cmd',
          root: { type: 'leaf', id: 'pane-operator', ptyStatus: 'running', label: 'cmd', shell: 'cmd', errorMessage: null, owner: 'user', observerEnabled: true },
        },
      ],
      activeTabId: 'tab-operator',
      focusedPaneId: 'pane-operator',
    });

    useTerminalStore.getState().handleRawMessage({
      type: 'terminal_started',
      data: {
        pane_id: 'pty_agent01',
        backend: 'winpty',
        agent_spawned: true,
        name: 'claude',
      },
    });

    const { tabs, activeTabId } = useTerminalStore.getState();
    expect(tabs).toHaveLength(2);
    // Operator's tab remains focused — new agent tab was appended but not activated.
    expect(activeTabId).toBe('tab-operator');
  });
});
