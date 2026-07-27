import { useEffect, useState, useRef, useCallback, useMemo } from 'react';
import { useTerminalStore } from '../stores/terminal';
import { useWebSocketStore } from '../stores/websocket';
import { useObserverStore } from '../stores/observer';
import type { PaneLeaf, PaneNode } from '../lib/types';
import { PaneTree } from '../lib/terminal/PaneTree';
import { TerminalSearch } from '../lib/terminal/TerminalSearch';
import { RecordingPlayer } from '../lib/terminal/RecordingPlayer';
import { dispatch as dispatchKey, DEFAULT_BINDINGS } from '../lib/terminal/keybindings';
import { Hint } from '../components/ui/Hint';

const TERMINAL_HELP_TEXT =
  'Shortcuts:\n' +
  '  Ctrl+Shift+K  new tab\n' +
  '  Ctrl+Shift+X  close tab\n' +
  '  Ctrl+Shift+D  split vertical\n' +
  '  Ctrl+Shift+E  split horizontal\n' +
  '  Alt+Shift+X   close focused pane\n' +
  '  Ctrl+PageUp / PageDown  switch tab\n' +
  '  Ctrl+F  search in pane\n' +
  '\nClick + to add a tab; the ∨ menu picks a shell. Click × on a tab to close it, or the × on a focused pane to close just that split. Drag the divider between split panes to resize.';

function findLeaf(node: PaneNode, id: string): PaneLeaf | null {
  if (node.type === 'leaf') return node.id === id ? node : null;
  return findLeaf(node.first, id) ?? findLeaf(node.second, id);
}

function firstLeaf(node: PaneNode): PaneLeaf {
  return node.type === 'leaf' ? node : firstLeaf(node.first);
}

// The real multi-tab terminal. SC-0 reverted the Y-3 surface-card host —
// `TerminalView` now mounts this directly (single instance), and SC-2's panel
// manager will host this same component unchanged inside a glass panel. The
// one-shot PTY bootstrap below fires once per mount.
export function TerminalPanes() {
  const tabs = useTerminalStore((s) => s.tabs);
  const activeTabId = useTerminalStore((s) => s.activeTabId);
  const focusedPaneId = useTerminalStore((s) => s.focusedPaneId);
  const config = useTerminalStore((s) => s.config);
  const replayingRecordingId = useTerminalStore((s) => s.replayingRecordingId);
  const wsStatus = useWebSocketStore((s) => s.status);
  const observerState = useObserverStore((s) => s.state);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [searchOpen, setSearchOpen] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);

  const activeTab = tabs.find((t) => t.id === activeTabId);
  const focusedLeaf: PaneLeaf | null =
    activeTab && focusedPaneId
      ? findLeaf(activeTab.root, focusedPaneId)
      : activeTab
      ? firstLeaf(activeTab.root)
      : null;

  // Phase 6 (terminal-control 2026-05-16) — observer-always-on. The
  // per-pane consent modal that used to gate observer access here was
  // removed: arming the observer (via the right-panel toggle) now
  // grants consent for every live + future pane atomically on the
  // backend (PTYManager._maybe_auto_grant_consent + grant_consent_for_
  // all_live). The single operator control is the right-panel arm/
  // disarm — no per-pane prompts.

  // One-shot bootstrap. With F2 §5i keepAlive, TerminalView mounts exactly
  // once for the lifetime of the WS connection — tab switches no longer
  // unmount it. The `bootstrapDone` ref makes the single-fire intent
  // explicit so a future StrictMode double-invoke or a re-introduced remount
  // path can't silently respawn every PTY twice.
  const bootstrapDoneRef = useRef(false);
  useEffect(() => {
    if (bootstrapDoneRef.current) return;
    bootstrapDoneRef.current = true;
    const store = useTerminalStore.getState();
    const finish = () => {
      const s = useTerminalStore.getState();
      if (s.tabs.length === 0) {
        s.addTab();
      } else {
        // Persisted layout rehydrated — respawn ptys for every leaf.
        s.bootstrapPanes();
      }
    };
    if (!store.config) {
      store.fetchConfig().then(finish);
    } else {
      finish();
    }
  }, []);

  useEffect(() => {
    if (!dropdownOpen) return;
    const handler = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setDropdownOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [dropdownOpen]);

  const addTab = useCallback((shell?: string) => {
    useTerminalStore.getState().addTab(shell);
    setDropdownOpen(false);
  }, []);
  const closeTab = useCallback((tabId: string) => useTerminalStore.getState().closeTab(tabId), []);
  const setActiveTab = useCallback((tabId: string) => useTerminalStore.getState().setActiveTab(tabId), []);
  const toggleObserver = useCallback(() => {
    if (!focusedLeaf) return;
    useTerminalStore.getState().sendObserverToggle(focusedLeaf.id, !focusedLeaf.observerEnabled);
  }, [focusedLeaf]);

  // Keybinding command map — reconstructed each render so closures capture
  // the latest focusedLeaf / activeTab.
  const commands = useMemo<Record<string, () => void>>(() => {
    const store = () => useTerminalStore.getState();
    const cycleTab = (dir: 1 | -1) => {
      const s = store();
      if (s.tabs.length < 2 || !s.activeTabId) return;
      const idx = s.tabs.findIndex((t) => t.id === s.activeTabId);
      if (idx < 0) return;
      const nextIdx = (idx + dir + s.tabs.length) % s.tabs.length;
      s.setActiveTab(s.tabs[nextIdx].id);
    };
    return {
      'terminal.newTab': () => store().addTab(),
      'terminal.closeTab': () => {
        const s = store();
        if (s.activeTabId) s.closeTab(s.activeTabId);
      },
      'terminal.splitVertical': () => {
        const leaf = focusedLeaf;
        if (leaf) store().splitPane(leaf.id, 'vertical');
      },
      'terminal.splitHorizontal': () => {
        const leaf = focusedLeaf;
        if (leaf) store().splitPane(leaf.id, 'horizontal');
      },
      'terminal.closePane': () => {
        const leaf = focusedLeaf;
        if (leaf) store().closePane(leaf.id);
      },
      'terminal.nextTab': () => cycleTab(1),
      'terminal.prevTab': () => cycleTab(-1),
      'terminal.search': () => {
        if (focusedLeaf) setSearchOpen(true);
      },
      'terminal.toggleObserver': () => {
        const leaf = focusedLeaf;
        if (leaf) store().sendObserverToggle(leaf.id, !leaf.observerEnabled);
      },
    };
  }, [focusedLeaf]);

  // Global keybinding listener — reads bindings from config, falls back to defaults.
  useEffect(() => {
    const bindings = config?.keybindings ?? DEFAULT_BINDINGS;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (searchOpen) {
          setSearchOpen(false);
          return;
        }
        if (useTerminalStore.getState().replayingRecordingId) {
          useTerminalStore.getState().closeReplay();
          return;
        }
      }
      dispatchKey(e, bindings, commands);
    };
    // Capture phase: fires before xterm's hidden textarea, so chord shortcuts
    // (Ctrl+Shift+D etc.) reach our dispatcher even when the terminal has focus.
    window.addEventListener('keydown', handler, true);
    return () => window.removeEventListener('keydown', handler, true);
  }, [config, commands, searchOpen]);

  const profiles = config?.shell_profiles ?? {};
  const profileEntries = Object.entries(profiles);
  const getIcon = (shell: string) => profiles[shell]?.icon ?? '⟩';

  // HUD observer is the master switch (off / armed / observing). The
  // per-pane toggle below is the secondary opt-in: once the operator
  // arms the master, individual panes can opt in. With the master off,
  // pane-level enabling is meaningless — disable the toggle and stop
  // claiming "TARS observing" so the two surfaces stay coherent.
  const observerEnabled = focusedLeaf?.observerEnabled ?? false;
  const masterOn = observerState !== 'off';
  const effectivelyObserving = masterOn && observerEnabled;

  // Ctrl+Shift+O — global observer arm/disarm toggle
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.ctrlKey && e.shiftKey && e.key === 'O') {
        e.preventDefault();
        const obs = useObserverStore.getState();
        if (obs.state === 'off') {
          obs.arm();
        } else {
          obs.disarm();
        }
      }
    };
    window.addEventListener('keydown', handler);
    return () => window.removeEventListener('keydown', handler);
  }, []);

  return (
    <div className="wt">
      <div className="wt-tabbar">
        <div className="wt-tabs">
          {tabs.map((tab) => (
            <div
              key={tab.id}
              className={`wt-tab${tab.id === activeTabId ? ' is-active' : ''}`}
              onClick={() => setActiveTab(tab.id)}
            >
              <span className="wt-tab-icon">{getIcon(tab.root.type === 'leaf' ? tab.root.shell : '')}</span>
              <span className="wt-tab-label">{tab.label}</span>
              <button
                className="wt-tab-close"
                onClick={(e) => { e.stopPropagation(); closeTab(tab.id); }}
              >
                ×
              </button>
            </div>
          ))}
        </div>

        <Hint label={TERMINAL_HELP_TEXT} position="bottom" maxWidth={340}>
          <button className="wt-help-btn" type="button" aria-label="Terminal shortcuts">?</button>
        </Hint>

        <div className="wt-new" ref={dropdownRef}>
          <button className="wt-new-btn" onClick={() => addTab()} title="New terminal">+</button>
          <button
            className="wt-new-dropdown-btn"
            onClick={() => setDropdownOpen(!dropdownOpen)}
            title="Select shell profile"
          >
            ∨
          </button>
          {dropdownOpen && (
            <div className="wt-dropdown">
              {profileEntries.map(([key, profile]) => (
                <button key={key} className="wt-dropdown-item" onClick={() => addTab(key)}>
                  <span className="wt-dropdown-icon">{profile.icon ?? '⟩'}</span>
                  <span>{profile.label}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="wt-body">
        {wsStatus !== 'connected' && wsStatus !== 'connecting' && (
          <div className="wt-overlay"><span className="t-caption">Reconnecting...</span></div>
        )}
        {activeTab ? (
          <PaneTree node={activeTab.root} />
        ) : (
          <div className="wt-empty"><span className="t-sub">No terminal open</span></div>
        )}
        {searchOpen && focusedLeaf && (
          <TerminalSearch paneId={focusedLeaf.id} onClose={() => setSearchOpen(false)} />
        )}
        {replayingRecordingId && (
          <RecordingPlayer
            recordingId={replayingRecordingId}
            onClose={() => useTerminalStore.getState().closeReplay()}
          />
        )}
      </div>

      <div className="wt-statusbar">
        {/* Per-pane toggle only appears once the master is armed — until
            then the HUD observer button is the canonical surface. This
            avoids the two-bar duplication the operator flagged. */}
        {focusedLeaf && masterOn && (
          <button
            className="wt-statusbar-btn"
            onClick={toggleObserver}
            title={
              observerEnabled
                ? 'Disable TARS observer for this pane (master stays armed)'
                : 'Enable TARS observer for this pane'
            }
          >
            {effectivelyObserving ? '◉ this pane' : '○ this pane'}
          </button>
        )}
        {effectivelyObserving && <span className="wt-observer-dot" title="TARS is observing">● TARS observing</span>}
        <span className="wt-statusbar-meta">
          {tabs.length} tab{tabs.length !== 1 ? 's' : ''}
        </span>
      </div>
    </div>
  );
}

// SC-0 — Terminal reverts to whole-view rendering (the Y-3 canvas-shell split
// is superseded by the spatial-cockpit panel model). TerminalView renders the
// real multi-tab terminal directly; SC-2's panel manager hosts this component
// unchanged inside a glass panel. The keep-mounted view-pane (App.tsx) still
// preserves the xterm WebGL canvas across tab switches.
export function TerminalView() {
  return <TerminalPanes />;
}
