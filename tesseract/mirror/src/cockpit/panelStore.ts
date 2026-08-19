// SC-2/SC-3 + panel-polish — the panel manager store. A tab summons its whole
// view as a movable / resizable glass panel over the orb (PanelHost mounts
// these into the `.panel-host` slot). SC-3 folds the Kernel / Lifeline rails in
// as **dockable** panels. Panel-polish adds **pin** (persist as the default
// layout) and **maximize** (fill the center stage between the rails).
//
// One panel per kind (id === kind). Panels are keep-mounted once opened and
// toggled via `open` (not removed) so terminal PTY / chat scrollback / rail
// state survive a close→reopen.
//
// `view`-state coupling: view-panel focus drives `useUIStore.setView(kind)` so
// the four `view` subscribers (terminal xterm-fit, HudChatInput collapse,
// viewSnapshot sync, programmatic nav) keep working. Rails do NOT drive `view`.
// `orb` is the orb home — `resetAll` closes UNPINNED view panels + re-docks
// rails + parks `view` on `'orb'` (pinned panels stay).

import { create } from "zustand";

import { useUIStore, type View } from "../stores/ui";
import { nextZ, surfacePeakZ } from "./zStack";

export type RailKind = "kernel" | "lifeline";
export type DockSide = "left" | "right";

// Panel kinds = every view tab except the `orb` home, plus the two rails.
export type PanelKind = Exclude<View, "orb"> | RailKind;

export const RAIL_KINDS: readonly RailKind[] = ["kernel", "lifeline"];
// Every view panel the registry can render. A saved layout is operator
// localStorage that outlives a rename (AS-5 retired `soul` for `identity`),
// so hydration filters against this list — a kind with no view component
// would mount an empty panel the operator can't identify or close.
export const VIEW_PANEL_KINDS: readonly Exclude<View, "orb">[] = [
  "autonomy",
  "pulse",
  "chat",
  "terminal",
  "schedule",
  "agents",
  "channels",
  "identity",
  "conscience",
  "workspace",
  "settings",
];

export function isKnownViewPanel(kind: unknown): kind is Exclude<View, "orb"> {
  return (
    typeof kind === "string" &&
    (VIEW_PANEL_KINDS as readonly string[]).includes(kind)
  );
}
// Docked rail width. Shared by PanelHost (dock geometry) and GlassPanel
// (per-rail min-width): the generic `.glass-panel` CSS floor is 340px, and
// a right-docked rail positioned for 282 but rendered at 340 overflowed
// the stage's right edge by the difference (2026-07-29 cosmetic bug).
export const RAIL_W = 282;
const RAIL_HOME: Record<RailKind, DockSide> = {
  kernel: "left",
  lifeline: "right",
};

export function isPanelKind(kind: View): kind is Exclude<View, "orb"> {
  return kind !== "orb";
}
export function isRailKind(kind: string): kind is RailKind {
  return kind === "kernel" || kind === "lifeline";
}

export interface PanelState {
  id: PanelKind;
  kind: PanelKind;
  x: number;
  y: number;
  w: number;
  h: number;
  z: number;
  open: boolean;
  // Rails dock to an edge until dragged off (a drag sets `dock: null`).
  dock: DockSide | null;
  // Pinned = locked in place (GlassPanel disables move + resize). Survives
  // `resetAll`; pinned VIEW panels also persist to the saved layout (so a locked
  // panel stays put across reloads too). Available on every panel incl. rails.
  pinned: boolean;
  // Maximized panels render at the stage-minus-rails rect (computed by
  // PanelHost); their stored x/y/w/h is the restore geometry.
  maximized: boolean;
  // Minimized = collapsed off-stage but kept open + mounted (PTY/scrollback
  // survive); reachable from the HUD "summoned panes" list.
  minimized: boolean;
  placed: boolean;
}

// The persisted shape of a view panel (localStorage; see layoutPersistence).
// Every OPEN panel — not just pinned ones — persists with its full state so a
// reload restores the whole workspace (which tabs were summoned + their layout).
export interface SavedPanel {
  kind: Exclude<View, "orb">;
  x: number;
  y: number;
  w: number;
  h: number;
  pinned: boolean;
  minimized: boolean;
  maximized: boolean;
}

// The persisted shape of a rail. Unlike view panels (only saved when pinned),
// rails persist their full state — `open` (hidden), `dock` (moved off-edge →
// null), `pinned`, and geometry (used only when floating) — so pin / hide /
// move all survive a reload.
export interface SavedRail {
  kind: RailKind;
  open: boolean;
  dock: DockSide | null;
  pinned: boolean;
  x: number;
  y: number;
  w: number;
  h: number;
}

interface PanelStore {
  panels: PanelState[];
  topZ: number;
  openPanel: (kind: View) => void;
  closePanel: (id: PanelKind) => void;
  focus: (id: PanelKind) => void;
  move: (id: PanelKind, x: number, y: number) => void;
  resize: (id: PanelKind, w: number, h: number) => void;
  place: (
    id: PanelKind,
    geom: { x: number; y: number; w: number; h: number },
  ) => void;
  togglePin: (id: PanelKind) => void;
  toggleMaximize: (id: PanelKind) => void;
  toggleMinimize: (id: PanelKind) => void;
  ensureRails: () => void;
  toggleRail: (id: RailKind) => void;
  hydratePanels: (saved: SavedPanel[]) => void;
  hydrateRails: (saved: SavedRail[]) => void;
  resetAll: () => void;
  /** Close every view panel and leave everything else alone. */
  closeAllViews: () => void;
  /** Put ONE rail back where it started, without touching the rest. */
  resetRail: (kind: RailKind) => void;
}

const Z_BASE = 10;
const RAIL_Z: Record<RailKind, number> = { kernel: 4, lifeline: 5 };

function topmostOpenViewKind(
  panels: PanelState[],
): Exclude<View, "orb"> | null {
  // Minimized panels are open (kept mounted) but off-stage — handing them
  // the active view would light a tab with nothing visible.
  const open = panels.filter(
    (p) => p.open && !p.minimized && !isRailKind(p.kind),
  );
  if (open.length === 0) return null;
  return open.reduce((a, b) => (b.z > a.z ? b : a)).kind as Exclude<
    View,
    "orb"
  >;
}

function newPanel(
  kind: Exclude<View, "orb">,
  z: number,
  extra?: Partial<PanelState>,
): PanelState {
  return {
    id: kind,
    kind,
    x: 0,
    y: 0,
    w: 0,
    h: 0,
    z,
    open: true,
    dock: null,
    pinned: false,
    maximized: false,
    minimized: false,
    placed: false,
    ...extra,
  };
}

export const usePanelStore = create<PanelStore>((set, get) => ({
  panels: [],
  topZ: Z_BASE,

  openPanel: (kind) => {
    if (!isPanelKind(kind)) {
      get().resetAll();
      return;
    }
    const existing = get().panels.find((p) => p.id === kind);
    // A second click on the already-active, already-open tab closes it
    // (returns to the bare orb home) instead of re-opening a no-op — a tab
    // that's open forever with no way to un-highlight it was the bug.
    if (
      useUIStore.getState().view === kind &&
      existing?.open &&
      !existing.minimized
    ) {
      get().closePanel(kind);
      return;
    }
    set((s) => {
      const z = nextZ();
      if (existing) {
        // Re-opening clears `minimized` too — clicking a tab for a minimized
        // panel must bring it back, not silently re-open it still-hidden.
        return {
          topZ: z,
          panels: s.panels.map((p) =>
            p.id === kind ? { ...p, open: true, minimized: false, z } : p,
          ),
        };
      }
      return { topZ: z, panels: [...s.panels, newPanel(kind, z)] };
    });
    useUIStore.getState().setView(kind);
  },

  focus: (id) => {
    const s = get();
    const target = s.panels.find((p) => p.id === id);
    if (!target) return;
    // A docked rail stays BENEATH the view panels; a floating rail / view panel
    // raises to the top (but a no-op when already on top among the open set).
    const isDockedRail = isRailKind(id) && target.dock !== null;
    if (!isDockedRail) {
      // Raise unless this panel already holds the highest z among all open
      // panels + any raised surface. `surfacePeakZ()` carries the surface-only
      // high-water mark so the comparison is cross-system aware without
      // importing surfacesStore (cycle-safe). Closed panels are excluded:
      // their stale z values never contribute to either side of the check.
      const openMaxZ = s.panels
        .filter((p) => p.open)
        .reduce((m, p) => Math.max(m, p.z), 0);
      if (target.z < Math.max(openMaxZ, surfacePeakZ())) {
        const z = nextZ();
        set({
          topZ: z,
          panels: s.panels.map((p) => (p.id === id ? { ...p, z } : p)),
        });
      }
    }
    if (!isRailKind(id)) useUIStore.getState().setView(id);
  },

  closePanel: (id) => {
    set((s) => ({
      panels: s.panels.map((p) => (p.id === id ? { ...p, open: false } : p)),
    }));
    if (!isRailKind(id)) {
      const next = topmostOpenViewKind(get().panels);
      useUIStore.getState().setView(next ?? "orb");
    }
  },

  move: (id, x, y) =>
    set((s) => ({
      panels: s.panels.map((p) =>
        p.id === id ? { ...p, x, y, dock: null } : p,
      ),
    })),

  resize: (id, w, h) =>
    set((s) => ({
      panels: s.panels.map((p) => (p.id === id ? { ...p, w, h } : p)),
    })),

  place: (id, geom) =>
    set((s) => ({
      panels: s.panels.map((p) =>
        p.id === id ? { ...p, ...geom, placed: true } : p,
      ),
    })),

  togglePin: (id) =>
    set((s) => ({
      panels: s.panels.map((p) =>
        p.id === id ? { ...p, pinned: !p.pinned } : p,
      ),
    })),

  toggleMaximize: (id) =>
    set((s) => ({
      panels: s.panels.map((p) =>
        p.id === id ? { ...p, maximized: !p.maximized } : p,
      ),
    })),

  // Minimize collapses a panel off-stage (kept open + mounted). Restoring
  // (minimized→false) raises it to the front and re-activates its view, so a
  // click in the HUD "summoned panes" list brings it straight back.
  toggleMinimize: (id) => {
    const target = get().panels.find((p) => p.id === id);
    if (!target) return;
    if (!target.minimized) {
      set((s) => ({
        panels: s.panels.map((p) =>
          p.id === id ? { ...p, minimized: true } : p,
        ),
      }));
      // Minimizing the focused view hands off the highlight like closePanel
      // does — a lit tab with its panel off-stage reads as a broken toggle.
      if (!isRailKind(id) && useUIStore.getState().view === id) {
        const next = topmostOpenViewKind(get().panels);
        useUIStore.getState().setView(next ?? "orb");
      }
      return;
    }
    // Restore raises to the front — read topZ INSIDE the set callback so the
    // bump stays atomic (matches openPanel / focus).
    set((s) => {
      const z = nextZ();
      return {
        topZ: z,
        panels: s.panels.map((p) =>
          p.id === id ? { ...p, minimized: false, open: true, z } : p,
        ),
      };
    });
    if (!isRailKind(id)) useUIStore.getState().setView(id);
  },

  ensureRails: () =>
    set((s) => {
      const missing = RAIL_KINDS.filter(
        (k) => !s.panels.some((p) => p.id === k),
      );
      if (missing.length === 0) return s;
      const rails = missing.map<PanelState>((k) => ({
        id: k,
        kind: k,
        x: 0,
        y: 0,
        w: 0,
        h: 0,
        z: RAIL_Z[k],
        open: true,
        dock: RAIL_HOME[k],
        pinned: false,
        maximized: false,
        minimized: false,
        placed: false,
      }));
      return { ...s, panels: [...s.panels, ...rails] };
    }),

  toggleRail: (id) =>
    set((s) => ({
      panels: s.panels.map((p) => (p.id === id ? { ...p, open: !p.open } : p)),
    })),

  // Re-open the operator's last-session panels at their saved geometry + state
  // (pinned / minimized / maximized) on boot — the full workspace, not just
  // pinned panels. Idempotent: skips kinds already present. `placed: true` so
  // PanelHost keeps the saved geometry instead of centering.
  hydratePanels: (saved) =>
    set((s) => {
      let z = s.topZ;
      const fresh = saved
        .filter(
          (sp) =>
            !isRailKind(sp.kind) && !s.panels.some((p) => p.id === sp.kind),
        )
        .map((sp) => {
          z = nextZ();
          return newPanel(sp.kind, z, {
            x: sp.x,
            y: sp.y,
            w: sp.w,
            h: sp.h,
            pinned: sp.pinned,
            minimized: sp.minimized,
            maximized: sp.maximized,
            placed: true,
          });
        });
      if (fresh.length === 0) return s;
      return { topZ: z, panels: [...s.panels, ...fresh] };
    }),

  // Restore saved rail state on boot — runs BEFORE `ensureRails` (which then
  // skips the rails already present, so the operator's hide/move/pin survives).
  // A floating rail (dock=null) keeps its saved geometry (`placed:true`); a
  // docked rail re-docks via PanelHost (`placed:false` → recompute RAIL_W).
  hydrateRails: (saved) =>
    set((s) => {
      const fresh = saved
        .filter(
          (sr) =>
            isRailKind(sr.kind) && !s.panels.some((p) => p.id === sr.kind),
        )
        .map<PanelState>((sr) => ({
          id: sr.kind,
          kind: sr.kind,
          x: sr.x,
          y: sr.y,
          w: sr.w,
          h: sr.h,
          z: RAIL_Z[sr.kind],
          open: sr.open,
          dock: sr.dock,
          pinned: sr.pinned,
          maximized: false,
          minimized: false,
          placed: sr.dock === null,
        }));
      if (fresh.length === 0) return s;
      return { ...s, panels: [...s.panels, ...fresh] };
    }),

  resetRail: (kind) => {
    set((s) => ({
      panels: s.panels.map((p) =>
        p.kind === kind
          ? {
              ...p,
              open: true,
              dock: RAIL_HOME[kind],
              placed: false,
              maximized: false,
              minimized: false,
            }
          : p,
      ),
    }));
  },

  closeAllViews: () => {
    // Deliberately NOT `resetAll`: that also re-docks and re-shows the rails,
    // which is a restore, and the operator asked for a close (2026-08-14).
    // Pinned panels close too — the button says "all", and pinning locks a
    // panel in place rather than making it permanent.
    set((s) => ({
      panels: s.panels.map((p) =>
        isRailKind(p.kind)
          ? p
          : { ...p, open: false, maximized: false, minimized: false },
      ),
    }));
    useUIStore.getState().setView("orb");
  },

  resetAll: () => {
    // Close every UNPINNED view panel; keep pinned panels; re-dock + re-show
    // the rails (clean orb home, but the operator's pinned layout persists).
    set((s) => ({
      panels: s.panels.map((p) => {
        if (isRailKind(p.kind)) {
          return {
            ...p,
            open: true,
            dock: RAIL_HOME[p.kind as RailKind],
            placed: false,
          };
        }
        if (p.pinned) return { ...p, maximized: false, minimized: false };
        return { ...p, open: false, maximized: false, minimized: false };
      }),
    }));
    useUIStore.getState().setView("orb");
  },
}));
