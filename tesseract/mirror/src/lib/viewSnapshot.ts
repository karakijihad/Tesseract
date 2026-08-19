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
import { usePanelStore } from '../cockpit/panelStore';
import { useSurfacesStore } from '../stores/surfaces';
import { usePulseStore } from '../stores/pulse';
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
  // Which panels are open, stacked how, and which has focus. Separate from
  // `view_state` because it needs no per-view builder — the Mirror is one
  // composited surface and panels are layers on it, so this describes a panel
  // added next year without anyone writing code for it.
  layers: Record<string, unknown>;
}

const TITLE_CHARS = 80;

// `redactSecrets` filters by KEY name, which cannot help a secret sitting in a
// title's VALUE — and a title is free text the assistant chose, riding every
// turn from the orb view. These are the shapes a secret actually takes when it
// ends up in one: a provider key prefix, a bearer token, a long unbroken
// high-entropy run. Conservative on purpose: a false positive costs a card
// title the model could have read, a false negative sends a key to a provider
// on every turn.
const SECRET_VALUE_RE =
  /(sk-[A-Za-z0-9_-]{8,}|Bearer\s+\S+|xox[baprs]-\S+|gh[pousr]_[A-Za-z0-9]{8,}|[A-Za-z0-9_\-]{40,})/;

function redactTitle(title: string): string {
  const trimmed = title.slice(0, TITLE_CHARS);
  return SECRET_VALUE_RE.test(trimmed) ? '[redacted title]' : trimmed;
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
  return { active_section: useSettingsStore.getState().activeSection };
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
  const { lastChatId } = useSessionStore.getState();
  return { chat_id: lastChatId ?? null };
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
  const entries = usePulseStore.getState().entries;
  const by_severity: Record<string, number> = {};
  for (const e of entries) {
    const sev = String(e.severity ?? 'unknown');
    by_severity[sev] = (by_severity[sev] ?? 0) + 1;
  }
  return { entry_count: entries.length, by_severity };
}

// The orb IS the canvas the assistant spawns cards onto, and this returned
// `{}` — so on the one view where its own work is visible, it was told
// nothing. Titles and types, not contents: what a card renders is the
// renderer's business and, for an `html` surface, sealed inside a sandboxed
// opaque origin that never reaches this process.
function _orbState(): Record<string, unknown> {
  const cards = useSurfacesStore.getState().surfacesFor('orb');
  return {
    surface_count: cards.length,
    surfaces: cards.map((d) => ({
      id: d.id,
      type: d.type,
      // Bounded: a title is free text the assistant chose, it rides on EVERY
      // turn from this view, and `redactSecrets` filters key names rather than
      // values — so a long or secret-bearing title would be sent whole,
      // repeatedly. Enough to identify the card, not enough to be a payload.
      title: d.title ? redactTitle(d.title) : null,
    })),
  };
}

// Which layers are open, stacked how, and which one has focus. Generic on
// purpose: it needs no function per view, so a panel added later is described
// without anyone remembering to describe it. Ordered front-most first — the
// operator working on something is looking at the top of the stack.
function _layers(): Record<string, unknown> {
  const panels = usePanelStore.getState().panels;
  const visible = panels
    .filter((p) => p.open && !p.minimized)
    .sort((a, b) => (b.z ?? 0) - (a.z ?? 0));
  return {
    focused: visible[0]?.id ?? null,
    open: visible.map((p) => ({
      id: p.id,
      z: p.z,
      maximized: p.maximized,
      pinned: p.pinned,
    })),
    minimized: panels.filter((p) => p.open && p.minimized).map((p) => p.id),
  };
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
  let layers: Record<string, unknown>;
  try {
    layers = _layers();
  } catch (err) {
    console.warn('[viewSnapshot] layers threw', err);
    layers = { __layers_error: true };
  }
  return { view, view_state: redactSecrets(raw), layers: redactSecrets(layers) };
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

// Layers are part of the signature, not just the payload. Without them,
// opening or focusing a panel produces an identical signature to the one
// before it and the emit is suppressed as a duplicate — so the presence cache
// would keep whatever layout it first saw.
function _signature(snap: ViewSnapshot): string {
  return JSON.stringify({ v: snap.view, s: snap.view_state, l: snap.layers });
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
        layers: snap.layers,
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
  // Panels are the other half of "where is the operator". Summoning,
  // focusing, minimising or closing one changes the answer without touching
  // `view` at all, and the route watcher above cannot see any of it — so the
  // presence cache used to describe a layout the operator had left behind.
  // The debounce collapses a drag or a burst of z-changes into one envelope.
  // Cards are the other thing that changes what the operator is looking at
  // without touching `view` or the panels: the assistant spawns one, or the
  // operator closes it, and the orb's own state block moves. Keyed on ids so a
  // geometry drag does not emit — the same reason the panel signature ignores
  // x/y/w/h.
  let lastSurfaceSignature = "";
  useSurfacesStore.subscribe((state) => {
    const signature = Object.keys(state.byView["orb"] ?? {}).sort().join("|");
    if (signature !== lastSurfaceSignature) {
      lastSurfaceSignature = signature;
      emitViewSnapshot();
    }
  });
  let lastLayerSignature = "";
  usePanelStore.subscribe((state) => {
    // Built without an intermediate array: this runs on EVERY panel-store
    // write, and a drag writes x/y/w/h at pointer-move frequency. None of
    // those fields are in the signature, so the work would be pure waste.
    let signature = "";
    for (const p of state.panels) {
      if (!p.open) continue;
      signature += `${p.id}:${p.z}:${p.minimized ? 1 : 0}:${p.maximized ? 1 : 0}|`;
    }
    if (signature !== lastLayerSignature) {
      lastLayerSignature = signature;
      emitViewSnapshot();
    }
  });
}
