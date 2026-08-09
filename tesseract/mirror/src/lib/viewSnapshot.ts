// Per-turn view-context snapshot. Read at chat_message send time and
// shipped on the WS envelope; the chat brain renders it as a
// `current_view` + `view_state` system block so questions like "help me
// with this row" resolve against whatever Mirror tab the operator is on.
//
// AU-21 extends this with `emitViewSnapshot()` — fired on route change
// (or view-internal focus) with a 500ms debounce so the autonomy layer
// can answer "where is the operator right now" without screenshots.
//
// Privacy gate (HARD RULE per MP-2 plan): any field whose key matches
// SECRET_KEY_RE is replaced with the literal string `[redacted]` before
// the snapshot leaves the browser. Walks nested objects and arrays.

import { useUIStore, type View } from '../stores/ui';
import { useSettingsStore } from '../stores/settings';
import { useAutonomyStore } from '../stores/autonomy';
import { useTerminalStore } from '../stores/terminal';
import { useAgentsStore } from '../stores/agents';
import { useScheduleStore } from '../stores/schedule';
import { useConscienceStore } from '../stores/conscience';
import { useChannelsStore } from '../stores/channels';
import { useWorkspaceStore } from '../stores/workspace';
import { useSoulStore } from '../stores/soul';
import { useSessionStore } from '../stores/session';
import { useWebSocketStore } from '../stores/websocket';

export interface ViewSnapshot {
  view: View;
  view_state: Record<string, unknown>;
}

const SECRET_KEY_RE = /(token|secret|password|api_?key|bot_?token)/i;
const REDACTED = '[redacted]';

export function redactSecrets<T>(value: T): T {
  if (Array.isArray(value)) {
    return value.map((v) => redactSecrets(v)) as unknown as T;
  }
  if (value && typeof value === 'object') {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      if (SECRET_KEY_RE.test(k)) {
        out[k] = REDACTED;
      } else {
        out[k] = redactSecrets(v);
      }
    }
    return out as unknown as T;
  }
  return value;
}

function _settingsState(): Record<string, unknown> {
  const { collapsedSections } = useSettingsStore.getState();
  const open = Object.entries(collapsedSections)
    .filter(([, collapsed]) => !collapsed)
    .map(([section]) => section);
  return { open_sections: open };
}

function _autonomyState(): Record<string, unknown> {
  const s = useAutonomyStore.getState();
  return {
    selected_agenda_id: s.selectedAgendaId ?? null,
    agenda_count: s.agenda.data.length,
    workers_count: s.workers.data.length,
    governor_running: s.governor.data?.running ?? false,
    pause_count: s.governor.data?.pauses?.length ?? 0,
  };
}

function _terminalState(): Record<string, unknown> {
  const s = useTerminalStore.getState();
  return {
    active_tab_id: s.activeTabId ?? null,
    focused_pane_id: s.focusedPaneId ?? null,
    tab_count: s.tabs.length,
  };
}

function _chatState(): Record<string, unknown> {
  const { saveName } = useSessionStore.getState();
  return { save_name: saveName ?? null };
}

function _scheduleState(): Record<string, unknown> {
  const s = useScheduleStore.getState();
  return { job_count: s.jobs.length };
}

function _agentsState(): Record<string, unknown> {
  const s = useAgentsStore.getState();
  return { agent_count: s.agents.length };
}

function _conscienceState(): Record<string, unknown> {
  const s = useConscienceStore.getState();
  const summary = s.report?.summary;
  return {
    bad: summary?.bad ?? 0,
    warn: summary?.warn ?? 0,
    ok: summary?.ok ?? 0,
  };
}

function _channelsState(): Record<string, unknown> {
  const s = useChannelsStore.getState();
  return { channel_count: s.channels.length };
}

function _workspaceState(): Record<string, unknown> {
  const s = useWorkspaceStore.getState();
  return {
    pending_count: s.events.filter((e) => e.status === 'pending').length,
    event_count: s.events.length,
  };
}

function _identityState(): Record<string, unknown> {
  const s = useSoulStore.getState();
  return { last_reflected_at: s.lastReflectedAt };
}

function _pulseState(): Record<string, unknown> {
  return {};
}

function _orbState(): Record<string, unknown> {
  return {};
}

const _STATE_BUILDERS: Record<View, () => Record<string, unknown>> = {
  autonomy: _autonomyState,
  orb: _orbState,
  chat: _chatState,
  terminal: _terminalState,
  pulse: _pulseState,
  identity: _identityState,
  schedule: _scheduleState,
  agents: _agentsState,
  conscience: _conscienceState,
  channels: _channelsState,
  workspace: _workspaceState,
  settings: _settingsState,
};

export function buildViewSnapshot(): ViewSnapshot {
  const view = useUIStore.getState().view;
  const builder = _STATE_BUILDERS[view];
  let raw: Record<string, unknown>;
  try {
    raw = builder ? builder() : {};
  } catch (err) {
    // A store throwing during snapshot build (rare, but a partial
    // hydration could expose it) must NOT block the chat turn nor the
    // emit path. Surface as an empty state with an inline marker.
    console.warn('[viewSnapshot] builder threw', err);
    raw = { __builder_error: true };
  }
  return { view, view_state: redactSecrets(raw) };
}

// AU-21 — debounced WS emit
//
// Fired on route change AND on intra-view focus mutations. The debounce
// collapses bursts (e.g. tab → modal-open → close-modal within 200ms)
// into a single envelope so the server gets the steady-state view rather
// than transient bounces.

const DEBOUNCE_MS = 500;

let _debounceTimer: ReturnType<typeof setTimeout> | null = null;
let _lastEmittedSignature: string | null = null;

function _signature(snap: ViewSnapshot): string {
  return JSON.stringify({ v: snap.view, s: snap.view_state });
}

export function emitViewSnapshot(): void {
  if (_debounceTimer !== null) {
    clearTimeout(_debounceTimer);
  }
  _debounceTimer = setTimeout(() => {
    _debounceTimer = null;
    const snap = buildViewSnapshot();
    const sig = _signature(snap);
    if (sig === _lastEmittedSignature) return;
    _lastEmittedSignature = sig;
    try {
      useWebSocketStore.getState().sendMessage('view_snapshot', {
        view: snap.view,
        view_state: snap.view_state,
        ts: new Date().toISOString(),
      });
    } catch (err) {
      console.warn('[viewSnapshot] emit failed', err);
    }
  }, DEBOUNCE_MS);
}

let _routeWatcherInstalled = false;

export function installViewSnapshotWatcher(): void {
  if (_routeWatcherInstalled) return;
  _routeWatcherInstalled = true;
  let lastView: View | null = null;
  useUIStore.subscribe((state) => {
    if (state.view !== lastView) {
      lastView = state.view;
      emitViewSnapshot();
    }
  });
}
