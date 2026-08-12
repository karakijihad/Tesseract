import { create } from 'zustand';
import { persist, createJSONStorage, type PersistOptions } from 'zustand/middleware';
import type { Terminal } from '@xterm/xterm';
import type { SearchAddon } from '@xterm/addon-search';
import type { PaneLeaf, PaneNode, PaneSplit, TerminalTab, TerminalConfig } from '../lib/types';
import { fetchTerminalConfig, setTerminalTheme, fetchControllerSessions } from '../lib/api';
import type { ControllerSession } from '../lib/api';
import { resolveTheme } from '../lib/terminal/theme';
import { useWebSocketStore } from './websocket';

// F2 (terminal daily-driver 2026-07-05) — per-pane byte watermark, the
// xterm.js-documented flow-control pattern (HIGH ceiling is a build-time
// UI constant, not backend config — the frontend build-time carve-out
// covers this tunable).
export const WATERMARK_HIGH = 100_000;
export const WATERMARK_LOW = 10_000;

// ── Helpers ─────────────────────────────────────────────

let _idCounter = 0;
function nextId(prefix: string): string {
  return `${prefix}-${++_idCounter}-${Date.now().toString(36)}`;
}

function makeLeaf(shell: string, label: string): PaneLeaf {
  return { type: 'leaf', id: nextId('pane'), ptyStatus: 'starting', label, shell, errorMessage: null, owner: 'user', observerEnabled: true, lastSeenCursor: 0 };
}

function makeAgentLeaf(paneId: string, shell: string, label: string): PaneLeaf {
  // Agent-spawned panes carry the backend-issued pane_id (e.g.
  // `pty_826d381498bf`) so the frontend can route output chunks back to
  // the same xterm instance the backend is streaming into. Owner stays
  // `entity` until the operator hands off.
  return {
    type: 'leaf',
    id: paneId,
    ptyStatus: 'running',
    label,
    shell,
    errorMessage: null,
    owner: 'entity',
    observerEnabled: true,
  };
}

function findNode(root: PaneNode, id: string): PaneNode | null {
  if (root.id === id) return root;
  if (root.type === 'split') {
    return findNode(root.first, id) || findNode(root.second, id);
  }
  return null;
}

function replaceNode(root: PaneNode, id: string, replacement: PaneNode): PaneNode {
  if (root.id === id) return replacement;
  if (root.type === 'split') {
    return {
      ...root,
      first: replaceNode(root.first, id, replacement),
      second: replaceNode(root.second, id, replacement),
    };
  }
  return root;
}

function removeNode(root: PaneNode, id: string): PaneNode | null {
  if (root.type === 'leaf') return root.id === id ? null : root;
  if (root.first.id === id) return root.second;
  if (root.second.id === id) return root.first;
  const firstResult = removeNode(root.first, id);
  if (firstResult !== root.first) {
    return firstResult === null ? root.second : { ...root, first: firstResult };
  }
  const secondResult = removeNode(root.second, id);
  if (secondResult !== root.second) {
    return secondResult === null ? root.first : { ...root, second: secondResult };
  }
  return root;
}

function collectLeaves(node: PaneNode): PaneLeaf[] {
  if (node.type === 'leaf') return [node];
  return [...collectLeaves(node.first), ...collectLeaves(node.second)];
}

function findLeaf(node: PaneNode, id: string): PaneLeaf | null {
  if (node.type === 'leaf') return node.id === id ? node : null;
  return findLeaf(node.first, id) ?? findLeaf(node.second, id);
}

function updateLeaf(root: PaneNode, id: string, patch: Partial<PaneLeaf>): PaneNode {
  if (root.type === 'leaf' && root.id === id) return { ...root, ...patch } as PaneLeaf;
  if (root.type === 'split') {
    return { ...root, first: updateLeaf(root.first, id, patch), second: updateLeaf(root.second, id, patch) };
  }
  return root;
}

// Rewrite every leaf in the tree to a "stopped, no-error" state. Used before
// persisting so rehydrated tabs never claim to have a live pty.
function sanitizeTreeForPersist(node: PaneNode): PaneNode {
  if (node.type === 'leaf') {
    return { ...node, ptyStatus: 'stopped', errorMessage: null };
  }
  return {
    ...node,
    first: sanitizeTreeForPersist(node.first),
    second: sanitizeTreeForPersist(node.second),
  };
}

// ── Store ───────────────────────────────────────────────

export type { ControllerSession };

interface TerminalState {
  tabs: TerminalTab[];
  activeTabId: string | null;
  config: TerminalConfig | null;
  _terms: Map<string, Terminal>;
  _searchAddons: Map<string, SearchAddon>;
  // F2 — per-pane buffered-byte watermark + whether a `terminal_pause`
  // is currently outstanding (so pause/resume are each sent exactly once
  // per threshold crossing, not per chunk).
  _watermarks: Map<string, number>;
  _pausedPanes: Set<string>;
  // Live-gate fix pass (2026-07-05) — panes currently writing a
  // server-flagged replay chunk (reattach). While a pane is in this set,
  // `sendKeystroke` drops onData emissions for it, since a replayed
  // terminal-query sequence (device attributes, DSR) makes xterm.js
  // auto-respond via onData — those synthetic responses must never reach
  // the shell's stdin.
  _replayingPanes: Set<string>;
  transcriptOpen: boolean;
  focusedPaneId: string | null;
  activeThemeName: string | null;
  replayingRecordingId: string | null;
  controllerSessions: ControllerSession[];

  fetchConfig(): Promise<void>;
  fetchControllerSessions(): Promise<void>;
  setActiveTheme(name: string): void;
  bootstrapPanes(): void;
  reattachAfterReconnect(): void;
  addTab(shell?: string): void;
  addRecordedTab(shell?: string): void;
  closeTab(tabId: string): void;
  setActiveTab(tabId: string): void;
  splitPane(paneId: string, direction: 'horizontal' | 'vertical', shell?: string): void;
  closePane(paneId: string): void;
  setFocusedPane(paneId: string): void;
  setPaneRatio(splitId: string, ratio: number): void;
  attachTerminal(paneId: string, term: Terminal, search?: SearchAddon): void;
  detachTerminal(paneId: string): void;
  getSearchAddon(paneId: string): SearchAddon | undefined;
  handleRawMessage(msg: Record<string, unknown>): void;
  sendStart(paneId: string, shell?: string, opts?: { record?: boolean }): void;
  sendKeystroke(paneId: string, data: string): void;
  /** AS-2 terminal mic mode — type a transcript into whichever pane has
   * focus. `typed` is false when there was no live pane, so the caller can
   * tell the operator rather than dropping their words; `sanitized` is true
   * when control characters were stripped, so a rewritten line is reported
   * rather than silently typed. */
  typeIntoFocusedPane(text: string): { typed: boolean; sanitized: boolean };
  sendResize(paneId: string, cols: number, rows: number): void;
  sendStop(paneId: string): void;
  sendObserverToggle(paneId: string, enabled: boolean): void;
  sendReattach(paneId: string, sinceToken: number, fresh?: boolean): void;
  toggleTranscript(): void;
  openReplay(recordingId: string): void;
  closeReplay(): void;
}

function _send(obj: Record<string, unknown>): void {
  useWebSocketStore.getState().sendRaw(obj);
}

// F2 — guard against a deadlocked pause: if a pane's term is disposed or
// reset while a `terminal_pause` is outstanding, the server would wait
// forever for a `terminal_resume` that can now never come (the watermark
// callback that would have sent it is gone with the disposed term).
function _resetPaneFlowControl(paneId: string): void {
  const store = useTerminalStore.getState();
  store._watermarks.delete(paneId);
  // A disposed/reset term never fires the write callback that would have
  // cleared this — don't leave the pane permanently unable to send keystrokes.
  store._replayingPanes.delete(paneId);
  if (store._pausedPanes.has(paneId)) {
    store._pausedPanes.delete(paneId);
    _send({ type: 'terminal_resume', pane_id: paneId });
  }
}

type SetState = (
  partial:
    | TerminalState
    | Partial<TerminalState>
    | ((state: TerminalState) => TerminalState | Partial<TerminalState>),
  replace?: false,
) => void;

function _spawnTab(
  get: () => TerminalState,
  set: SetState,
  shell: string | undefined,
  record: boolean,
): void {
  const { config, tabs } = get();
  const maxTabs = config?.max_tabs ?? 8;
  if (tabs.length >= maxTabs) return;

  const shellName = shell || config?.default_shell || 'cmd';
  const profile = config?.shell_profiles?.[shellName];
  const label = profile?.label || shellName;
  const leaf = makeLeaf(shellName, label);
  const tab: TerminalTab = { id: nextId('tab'), label, root: leaf };

  set((s) => ({
    tabs: [...s.tabs, tab],
    activeTabId: tab.id,
    focusedPaneId: leaf.id,
  }));

  get().sendStart(leaf.id, shellName, { record });
}

// F6 live-gate fix pass (2026-07-05) — shared per-pane reattach handshake.
// `fresh: true` (page bootstrap/reload — every xterm instance is brand new,
// no content) always requests a full buffer replay, ignoring any persisted
// `lastSeenCursor` (Finding 1: a cursor from a previous page lifetime must
// never suppress replay into a blank terminal). `fresh: false` (WS
// reconnect while the page stayed open — Finding 2) requests a gap-only
// replay against the still-populated xterm, and resets any outstanding
// client-side flow-control state since the server resets its own pause
// state on every reattach.
function _reattachAllPanes(get: () => TerminalState, set: SetState, fresh: boolean): void {
  const { tabs } = get();
  if (tabs.length === 0) return;
  const paneCommands: { id: string; cursor: number }[] = [];
  const walk = (node: PaneNode) => {
    if (node.type === 'leaf') {
      paneCommands.push({ id: node.id, cursor: node.lastSeenCursor ?? 0 });
    } else {
      walk(node.first);
      walk(node.second);
    }
  };
  for (const tab of tabs) walk(tab.root);
  // Flip every leaf to 'starting' so the UI reflects the transient state
  // until the server's terminal_reattached / terminal_started event arrives.
  set({
    tabs: tabs.map((t) => ({
      ...t,
      root: (function flip(n: PaneNode): PaneNode {
        if (n.type === 'leaf') return { ...n, ptyStatus: 'starting', errorMessage: null };
        return { ...n, first: flip(n.first), second: flip(n.second) };
      })(t.root),
    })),
  });
  for (const p of paneCommands) {
    if (!fresh) _resetPaneFlowControl(p.id);
    get().sendReattach(p.id, p.cursor, fresh);
  }
}

type PersistedTerminalState = Pick<
  TerminalState,
  'tabs' | 'activeTabId' | 'activeThemeName' | 'focusedPaneId' | 'transcriptOpen'
>;

const _persistOptions: PersistOptions<TerminalState, PersistedTerminalState> = {
  name: 'tesseract:terminal:v1',
  version: 1,
  storage: createJSONStorage(() => localStorage),
  partialize: (s) => ({
    tabs: s.tabs.map((t) => ({ ...t, root: sanitizeTreeForPersist(t.root) })),
    activeTabId: s.activeTabId,
    activeThemeName: s.activeThemeName,
    focusedPaneId: s.focusedPaneId,
    transcriptOpen: s.transcriptOpen,
  }),
  // On version mismatch, drop stored state cleanly rather than carrying
  // forward a schema the new code can't interpret.
  migrate: (_persisted, version) => {
    if (version !== 1) return undefined as unknown as PersistedTerminalState;
    return _persisted as PersistedTerminalState;
  },
};

export const useTerminalStore = create<TerminalState>()(persist((set, get) => ({
  tabs: [],
  activeTabId: null,
  config: null,
  _terms: new Map(),
  _searchAddons: new Map(),
  _watermarks: new Map(),
  _pausedPanes: new Set(),
  _replayingPanes: new Set(),
  transcriptOpen: false,
  focusedPaneId: null,
  activeThemeName: null,
  replayingRecordingId: null,
  controllerSessions: [],

  async fetchConfig() {
    try {
      const cfg = await fetchTerminalConfig();
      set({ config: cfg, activeThemeName: cfg.active_theme ?? 'mirror' });
    } catch {
      // Fallback if backend unreachable
      set({
        config: {
          default_shell: 'cmd',
          shell_profiles: {
            cmd: { argv: ['cmd.exe'], label: 'Command Prompt' },
            bash: { argv: ['bash'], label: 'Bash' },
            claude: { argv: ['claude'], label: 'Claude CLI' },
          },
          max_panes_per_tab: 4,
          max_tabs: 8,
        },
      });
    }
  },

  async fetchControllerSessions() {
    try {
      const res = await fetchControllerSessions();
      set({ controllerSessions: res.sessions });
    } catch {
      // Non-fatal: leave existing list intact on transient failure.
    }
  },

  addTab(shell?: string) {
    _spawnTab(get, set, shell, false);
  },

  addRecordedTab(shell?: string) {
    _spawnTab(get, set, shell, true);
  },

  closeTab(tabId: string) {
    const { tabs, _terms } = get();
    const tab = tabs.find((t) => t.id === tabId);
    if (!tab) return;

    // Stop all PTYs in this tab
    const leaves = collectLeaves(tab.root);
    for (const leaf of leaves) {
      if (leaf.ptyStatus === 'running' || leaf.ptyStatus === 'starting') {
        get().sendStop(leaf.id);
      }
      const term = _terms.get(leaf.id);
      if (term) {
        term.dispose();
        _terms.delete(leaf.id);
      }
      _resetPaneFlowControl(leaf.id);
    }

    const remaining = tabs.filter((t) => t.id !== tabId);
    const nextTab = remaining.length > 0 ? remaining[remaining.length - 1] : null;
    const nextPaneId = nextTab ? collectLeaves(nextTab.root)[0]?.id ?? null : null;
    set({
      tabs: remaining,
      activeTabId: nextTab?.id ?? null,
      focusedPaneId: nextPaneId,
    });
    // Phase 16 — auto-focus surviving tab's terminal
    if (nextPaneId) get()._terms.get(nextPaneId)?.focus();
  },

  setActiveTab(tabId: string) {
    const tab = get().tabs.find((t) => t.id === tabId);
    if (!tab) return;
    const firstLeaf = collectLeaves(tab.root)[0];
    set({ activeTabId: tabId, focusedPaneId: firstLeaf?.id ?? null });
    // Phase 16 — auto-focus the terminal in the new tab
    if (firstLeaf) get()._terms.get(firstLeaf.id)?.focus();
  },

  splitPane(paneId: string, direction: 'horizontal' | 'vertical', shell?: string) {
    const { tabs, activeTabId, config } = get();
    const tab = tabs.find((t) => t.id === activeTabId);
    if (!tab) return;

    const maxPanes = config?.max_panes_per_tab ?? 4;
    if (collectLeaves(tab.root).length >= maxPanes) return;

    const existing = findNode(tab.root, paneId);
    if (!existing || existing.type !== 'leaf') return;

    const shellName = shell || config?.default_shell || 'cmd';
    const profile = config?.shell_profiles?.[shellName];
    const label = profile?.label || shellName;
    const newLeaf = makeLeaf(shellName, label);

    const split: PaneSplit = {
      type: 'split',
      id: nextId('split'),
      direction,
      ratio: 0.5,
      first: existing,
      second: newLeaf,
    };

    const newRoot = replaceNode(tab.root, paneId, split);
    set({
      tabs: tabs.map((t) => (t.id === tab.id ? { ...t, root: newRoot } : t)),
      focusedPaneId: newLeaf.id,
    });
    // Phase 16 — auto-focus the new pane after split
    requestAnimationFrame(() => get()._terms.get(newLeaf.id)?.focus());

    get().sendStart(newLeaf.id, shellName);
  },

  closePane(paneId: string) {
    const { tabs, activeTabId, _terms } = get();
    const tab = tabs.find((t) => t.id === activeTabId);
    if (!tab) return;

    const leaves = collectLeaves(tab.root);
    if (leaves.length <= 1) {
      // Last pane — close the whole tab
      get().closeTab(tab.id);
      return;
    }

    const leaf = leaves.find((l) => l.id === paneId);
    if (leaf && (leaf.ptyStatus === 'running' || leaf.ptyStatus === 'starting')) {
      get().sendStop(paneId);
    }

    const term = _terms.get(paneId);
    if (term) {
      term.dispose();
      _terms.delete(paneId);
    }
    _resetPaneFlowControl(paneId);

    const newRoot = removeNode(tab.root, paneId);
    if (!newRoot) return;

    const newLeaves = collectLeaves(newRoot);
    const survivorId = newLeaves[0]?.id ?? null;
    set({
      tabs: tabs.map((t) => (t.id === tab.id ? { ...t, root: newRoot } : t)),
      focusedPaneId: survivorId,
    });
    // Phase 16 — auto-focus the surviving pane after close
    if (survivorId) get()._terms.get(survivorId)?.focus();
  },

  setFocusedPane(paneId: string) {
    set({ focusedPaneId: paneId });
    get()._terms.get(paneId)?.focus();
  },

  setPaneRatio(splitId: string, ratio: number) {
    const { tabs, activeTabId } = get();
    const tab = tabs.find((t) => t.id === activeTabId);
    if (!tab) return;

    const clamped = Math.max(0.15, Math.min(0.85, ratio));
    const node = findNode(tab.root, splitId);
    if (!node || node.type !== 'split') return;

    const updated: PaneSplit = { ...node, ratio: clamped };
    const newRoot = replaceNode(tab.root, splitId, updated);
    set({ tabs: tabs.map((t) => (t.id === tab.id ? { ...t, root: newRoot } : t)) });
  },

  attachTerminal(paneId: string, term: Terminal, search?: SearchAddon) {
    get()._terms.set(paneId, term);
    if (search) get()._searchAddons.set(paneId, search);
  },

  detachTerminal(paneId: string) {
    get()._terms.delete(paneId);
    get()._searchAddons.delete(paneId);
    _resetPaneFlowControl(paneId);
  },

  getSearchAddon(paneId: string) {
    return get()._searchAddons.get(paneId);
  },

  setActiveTheme(name: string) {
    const { config, _terms, activeThemeName } = get();
    const themeCfg = config?.themes?.[name];
    if (!themeCfg && name !== 'mirror') return; // unknown theme — no-op
    set({ activeThemeName: name });
    const resolved = resolveTheme(themeCfg ?? null);
    for (const term of _terms.values()) {
      term.options.theme = resolved;
    }
    if (name !== activeThemeName) {
      // Best-effort server persistence. UI already reflects the change.
      setTerminalTheme(name).catch((err) => {
        console.warn('[terminal] failed to persist theme to server:', err);
      });
    }
  },

  bootstrapPanes() {
    // F6 (terminal daily-driver 2026-07-05) — ask to REATTACH every
    // persisted leaf's pty first (it may still be alive server-side,
    // inside its grace window from the previous WS drop / page reload);
    // only respawn (the old unconditional behavior) if the backend
    // reports the pane is gone (`terminal_reattach_failed`).
    //
    // Live-gate fix pass — this runs exactly when every xterm instance is
    // brand new (page bootstrap/reload), so it always asks for a FULL
    // replay (`fresh: true`), never the persisted `lastSeenCursor`. That
    // cursor survives a reload (see `sanitizeTreeForPersist`) but the
    // fresh xterm it would be paired with has no content — a gap-only
    // replay against a blank terminal left the pane looking empty.
    _reattachAllPanes(get, set, true);
  },

  // Live-gate fix pass (Finding 2) — the reattach/respawn handshake above
  // only ran once, on page mount. A WS reconnect (e.g. the backend process
  // restarting) never re-triggered it, so a pane a client had open when the
  // WS dropped stayed dead after the socket came back: keystrokes went
  // nowhere, no respawn, no indication anything was wrong. `websocket.ts`
  // calls this on every reconnect (but not the very first connect, which
  // `bootstrapPanes` above already covers) so every open pane either
  // reattaches to its still-running process or — when the backend reports
  // it gone, e.g. because the backend itself restarted — falls back to
  // respawning a fresh shell via the existing `terminal_reattach_failed`
  // handler. The xterm still has its prior content here (the page never
  // reloaded), so this asks for a gap-only replay, not a full one.
  reattachAfterReconnect() {
    _reattachAllPanes(get, set, false);
  },

  handleRawMessage(msg: Record<string, unknown>) {
    const type = msg.type as string;
    const data = (msg.data ?? msg) as Record<string, unknown>;
    const paneId = (data.pane_id as string) || 'default';

    switch (type) {
      case 'terminal_output_chunk': {
        const bytes = data.bytes as string;
        const term = get()._terms.get(paneId);

        // Live-gate fix pass (2026-07-05) — `replay: true` marks a
        // reattach replay chunk (`pty_manager.py::_reattach`). Those
        // bytes can contain terminal query sequences the shell/ConPTY
        // emitted live (device attributes ESC[c, DSR ESC[6n); xterm.js
        // auto-answers them via `onData` while re-parsing, and those
        // synthetic answers must never reach the shell's stdin (the
        // exit-gate observed the literal echo `^[[?1;2c` corrupting the
        // operator's first typed command after a reload). Mark the pane
        // "replaying" for the duration of this write so `sendKeystroke`
        // drops onData emissions for it — dropped, not buffered: query
        // responses carry no state worth preserving, and the write
        // completes well within one JS task tick, so genuine user typing
        // racing this window is not a realistic concern.
        const isReplay = data.replay === true;
        if (isReplay) get()._replayingPanes.add(paneId);

        // F2 — byte watermark (xterm.js-documented flow-control pattern).
        // watermark tracks bytes handed to xterm that haven't finished
        // parsing/rendering yet (the term.write callback fires once they
        // have). Pause/resume each fire exactly once per crossing.
        const prevWatermark = get()._watermarks.get(paneId) ?? 0;
        const nextWatermark = prevWatermark + bytes.length;
        get()._watermarks.set(paneId, nextWatermark);
        if (term) {
          term.write(bytes, () => {
            if (isReplay) get()._replayingPanes.delete(paneId);
            const drained = Math.max((get()._watermarks.get(paneId) ?? 0) - bytes.length, 0);
            get()._watermarks.set(paneId, drained);
            if (drained < WATERMARK_LOW && get()._pausedPanes.has(paneId)) {
              get()._pausedPanes.delete(paneId);
              _send({ type: 'terminal_resume', pane_id: paneId });
            }
          });
        } else {
          // No term attached (e.g. an agent-spawned pane's tab not yet
          // rendered) — nothing buffers, so the watermark clears
          // immediately. Nothing is parsing this pane's bytes either, so
          // the replay guard has nothing to protect here.
          if (isReplay) get()._replayingPanes.delete(paneId);
          get()._watermarks.set(paneId, Math.max(nextWatermark - bytes.length, 0));
        }
        if (nextWatermark > WATERMARK_HIGH && !get()._pausedPanes.has(paneId)) {
          get()._pausedPanes.add(paneId);
          _send({ type: 'terminal_pause', pane_id: paneId });
        }

        // F6 — advance this pane's last-seen cursor so a future reattach
        // (page reload) only replays the gap, not the whole buffer again.
        const { tabs } = get();
        const currentLeaf = tabs.map((t) => findLeaf(t.root, paneId)).find((l) => l !== null) ?? null;
        const newCursor = (currentLeaf?.lastSeenCursor ?? 0) + bytes.length;
        set({
          tabs: tabs.map((t) => ({
            ...t,
            root: updateLeaf(t.root, paneId, { lastSeenCursor: newCursor }),
          })),
        });

        // Promote starting → running on first output (skip if already running)
        for (const t of tabs) {
          const node = findNode(t.root, paneId);
          if (node && node.type === 'leaf' && node.ptyStatus !== 'running') {
            set({
              tabs: get().tabs.map((tab) => ({
                ...tab,
                root: updateLeaf(tab.root, paneId, { ptyStatus: 'running' }),
              })),
            });
            break;
          }
        }
        break;
      }
      case 'terminal_reattached': {
        const obsEnabled = data.observer_enabled as boolean | undefined;
        set({
          tabs: get().tabs.map((t) => ({
            ...t,
            root: updateLeaf(t.root, paneId, {
              ptyStatus: 'running',
              errorMessage: null,
              ...(obsEnabled !== undefined ? { observerEnabled: obsEnabled } : {}),
            }),
          })),
        });
        break;
      }
      case 'terminal_reattach_failed': {
        // Backend says the pane is gone (grace expired / backend
        // restarted) — fall back to the pre-F6 respawn behavior.
        const { tabs } = get();
        const leaf = tabs.map((t) => findLeaf(t.root, paneId)).find((l) => l !== null);
        get().sendStart(paneId, leaf?.shell);
        break;
      }
      case 'terminal_started': {
        const backend = data.backend as string;
        const agentSpawned = data.agent_spawned === true;
        const agentName = typeof data.name === 'string' ? (data.name as string) : '';
        const defaultLabel = backend === 'winpty' ? 'cmd' : 'bash';
        const obsEnabled = data.observer_enabled as boolean | undefined;

        // Agent-spawned viewer panes don't pre-exist in the frontend
        // tree (`start_controller_session` / boot-time reattach opened
        // the pane on the backend; there's no matching tab here yet).
        // Without this branch, the pane bytes stream into the WS but
        // `updateLeaf` is a no-op and the pane is invisible — operator
        // complaint 2026-05-15.
        if (agentSpawned) {
          const { tabs, activeTabId } = get();
          const alreadyExists = tabs.some((t) => findNode(t.root, paneId) !== null);
          if (!alreadyExists) {
            const label = agentName || defaultLabel;
            const leaf = makeAgentLeaf(paneId, agentName || backend || 'agent', label);
            const tab: TerminalTab = { id: nextId('tab'), label, root: leaf };
            // 2026-05-16 — don't yank operator focus. Pre-fix this set
            // `activeTabId: tab.id`, so the operator's current tab
            // visibly "disappeared" the moment the assistant opened a delegate
            // pane. The pane is still appended + accessible; the
            // operator clicks to inspect. Only adopt the new tab when
            // there are no tabs at all (first-ever pane).
            const shouldFocusNew = tabs.length === 0;
            set({
              tabs: [...tabs, tab],
              activeTabId: shouldFocusNew ? tab.id : activeTabId,
              ...(shouldFocusNew ? { focusedPaneId: leaf.id } : {}),
            });
            break;
          }
        }

        set({
          tabs: get().tabs.map((t) => ({
            ...t,
            root: updateLeaf(t.root, paneId, {
              ptyStatus: 'running',
              label: (findNode(t.root, paneId) as PaneLeaf | null)?.label || defaultLabel,
              ...(obsEnabled !== undefined ? { observerEnabled: obsEnabled } : {}),
            }),
          })),
        });
        break;
      }
      case 'terminal_stopped': {
        set({
          tabs: get().tabs.map((t) => ({
            ...t,
            root: updateLeaf(t.root, paneId, { ptyStatus: 'stopped' }),
          })),
        });
        break;
      }
      case 'terminal_error': {
        const errorMsg = data.message as string;
        set({
          tabs: get().tabs.map((t) => ({
            ...t,
            root: updateLeaf(t.root, paneId, { ptyStatus: 'error', errorMessage: errorMsg }),
          })),
        });
        break;
      }
      case 'terminal_observer_status': {
        const obsEnabled = data.enabled as boolean;
        set({
          tabs: get().tabs.map((t) => ({
            ...t,
            root: updateLeaf(t.root, paneId, { observerEnabled: obsEnabled }),
          })),
        });
        break;
      }
    }
  },

  sendStart(paneId: string, shell?: string, opts?: { record?: boolean }) {
    _send({
      type: 'terminal_start',
      pane_id: paneId,
      shell: shell || undefined,
      record: opts?.record ?? false,
    });
  },

  sendKeystroke(paneId: string, data: string) {
    // Live-gate fix pass — drop onData emissions fired while this pane
    // is writing a replay chunk. See `_replayingPanes` above: a replayed
    // terminal-query sequence's synthetic xterm.js response must never
    // reach the shell's stdin.
    if (get()._replayingPanes.has(paneId)) return;
    _send({ type: 'terminal_keystroke', pane_id: paneId, bytes: data });
  },

  typeIntoFocusedPane(text: string) {
    // An STT transcript is untrusted text on its way to a live shell's
    // stdin. `.trim()` alone guards only the trailing edge — an EMBEDDED
    // CR or LF is submitted by the pty's line discipline, so a transcript
    // like "ls\nrm -rf ~" executes the first half no matter how careful
    // the no-trailing-newline rule is. Strip every C0/C1 control (which
    // also covers ESC, i.e. terminal escape sequences), fold the
    // line-breaking ones to a space so words don't fuse, and collapse.
    const trimmed = (text ?? '')
      // eslint-disable-next-line no-control-regex
      .replace(/[\r\n\t\v\f\u0085\u2028\u2029]+/g, ' ')
      // eslint-disable-next-line no-control-regex
      .replace(/[\u0000-\u001F\u007F-\u009F]/g, '')
      // Bidi overrides and zero-width formatting. Stripping C0/C1 alone
      // leaves these, and they defeat the safeguard the whole mode rests
      // on: the operator READS the line before pressing Enter, so a
      // U+202E makes the line they approve differ from the line that runs.
      .replace(/[\u200B-\u200F\u202A-\u202E\u2066-\u2069\uFEFF]/g, '')
      .replace(/\s+/g, ' ')
      .trim();
    // The operator said something this does not say — two sentences fused
    // into one command line reads as a typo they made, not a rewrite we
    // performed, unless we say so.
    const sanitized = trimmed !== (text ?? '').trim();
    if (!trimmed) return { typed: false, sanitized: false };
    const { focusedPaneId, tabs, activeTabId } = get();
    // Fall back to the active tab's first leaf: `focusedPaneId` is null
    // until something has been clicked, and an operator who just opened
    // the terminal and started talking still means "that one".
    const activeTab = tabs.find((tab) => tab.id === activeTabId);
    const focused =
      focusedPaneId && activeTab ? findLeaf(activeTab.root, focusedPaneId) : null;
    const leaf = focused ?? (activeTab ? (collectLeaves(activeTab.root)[0] ?? null) : null);
    // A pane whose pty is stopped/errored accepts the keystroke envelope and
    // drops it — so without this check the caller is told the words landed
    // and the operator watches them vanish. Returning false is what buys
    // them the "no open pane" toast.
    if (!leaf || (leaf.ptyStatus !== 'running' && leaf.ptyStatus !== 'starting')) {
      return { typed: false, sanitized };
    }
    const paneId = leaf.id;
    // No trailing newline, ever. This types the command and leaves the
    // cursor on it; a mis-transcribed word should cost a backspace, not
    // an executed command. The operator presses Enter.
    get().sendKeystroke(paneId, trimmed);
    get()._terms.get(paneId)?.focus();
    return { typed: true, sanitized };
  },

  sendResize(paneId: string, cols: number, rows: number) {
    _send({ type: 'terminal_resize', pane_id: paneId, cols, rows });
  },

  sendStop(paneId: string) {
    _send({ type: 'terminal_stop', pane_id: paneId });
  },

  sendObserverToggle(paneId: string, enabled: boolean) {
    _send({ type: 'terminal_observer_toggle', pane_id: paneId, enabled });
  },

  sendReattach(paneId: string, sinceToken: number, fresh = false) {
    // `fresh: true` — the xterm this pane will be paired with has no
    // content (page bootstrap/reload); since_token is omitted so the
    // backend always replays its full buffer regardless of what cursor
    // this pane happened to persist from a previous page lifetime.
    _send({
      type: 'terminal_reattach',
      pane_id: paneId,
      since_token: fresh ? null : String(sinceToken),
      fresh,
    });
  },

  toggleTranscript() {
    set((s) => ({ transcriptOpen: !s.transcriptOpen }));
  },

  openReplay(recordingId: string) {
    set({ replayingRecordingId: recordingId });
  },

  closeReplay() {
    set({ replayingRecordingId: null });
  },
}), _persistOptions));
