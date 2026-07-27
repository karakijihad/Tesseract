// The operator's cockpit layout persists to localStorage (operator-private,
// per-machine — matches the gitignored-workspace ethos; a backend sync can
// layer on later). The WHOLE workspace is saved, not just pinned panels:
//   • every OPEN view panel (which tabs were summoned) + geometry + pinned /
//     minimized / maximized state.
//   • the Kernel / Lifeline RAILS — open/hidden, dock, pinned, geometry.
// (Surfaces — lane cards, drawn html/image/code — persist separately, backend
// side, in canvas-state/<view>.json.) On boot we hydrate both; on every store
// change we debounce-save.

import {
  usePanelStore,
  isRailKind,
  type DockSide,
  type PanelState,
  type RailKind,
  type SavedPanel,
  type SavedRail,
} from './panelStore';

// v3: panels carry pinned/minimized/maximized (v2 persisted pinned-only with no
// flags). A bumped key starts clean rather than mis-reading the old shape.
const KEY = 'tesseract.cockpit.layout.v3';

interface SavedLayout {
  panels: SavedPanel[];
  rails: SavedRail[];
}

function isFiniteNum(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

function isRailName(v: unknown): v is RailKind {
  return v === 'kernel' || v === 'lifeline';
}

function isDock(v: unknown): v is DockSide | null {
  return v === 'left' || v === 'right' || v === null;
}

function sanitizePanels(value: unknown): SavedPanel[] {
  if (!Array.isArray(value)) return [];
  return value.filter((e): e is SavedPanel => {
    if (!e || typeof e !== 'object') return false;
    const r = e as Record<string, unknown>;
    return (
      typeof r.kind === 'string' &&
      r.kind !== 'tars' &&
      !isRailKind(r.kind) &&
      typeof r.pinned === 'boolean' &&
      typeof r.minimized === 'boolean' &&
      typeof r.maximized === 'boolean' &&
      isFiniteNum(r.x) &&
      isFiniteNum(r.y) &&
      isFiniteNum(r.w) &&
      isFiniteNum(r.h) &&
      (r.w as number) > 0 &&
      (r.h as number) > 0
    );
  });
}

function sanitizeRails(value: unknown): SavedRail[] {
  if (!Array.isArray(value)) return [];
  return value.filter((e): e is SavedRail => {
    if (!e || typeof e !== 'object') return false;
    const r = e as Record<string, unknown>;
    return (
      isRailName(r.kind) &&
      typeof r.open === 'boolean' &&
      isDock(r.dock) &&
      typeof r.pinned === 'boolean' &&
      isFiniteNum(r.x) &&
      isFiniteNum(r.y) &&
      isFiniteNum(r.w) &&
      isFiniteNum(r.h) &&
      // A floating rail must carry real geometry (PanelHost won't re-place a
      // `placed` panel); a docked rail is re-placed regardless, so 0×0 is fine.
      (r.dock !== null || ((r.w as number) > 0 && (r.h as number) > 0))
    );
  });
}

function readLayout(): SavedLayout {
  try {
    const raw = localStorage.getItem(KEY);
    if (raw) {
      const parsed: unknown = JSON.parse(raw);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        const o = parsed as Record<string, unknown>;
        return { panels: sanitizePanels(o.panels), rails: sanitizeRails(o.rails) };
      }
    }
  } catch {
    // Corrupt / unavailable storage — start clean.
  }
  return { panels: [], rails: [] };
}

/** Saved view panels — back-compat surface for callers/tests. */
export function loadSavedLayout(): SavedPanel[] {
  return readLayout().panels;
}

function saveLayout(panels: PanelState[]): void {
  // Persist every OPEN, placed view panel (the whole summoned set) with its
  // full state — not just pinned ones — so a reload restores the workspace.
  const open: SavedPanel[] = panels
    .filter((p) => p.open && !isRailKind(p.kind) && p.placed)
    .map((p) => ({
      kind: p.kind as SavedPanel['kind'],
      x: p.x,
      y: p.y,
      w: p.w,
      h: p.h,
      pinned: p.pinned,
      minimized: p.minimized,
      maximized: p.maximized,
    }));
  const rails: SavedRail[] = panels
    .filter((p) => isRailKind(p.kind))
    .map((p) => ({
      kind: p.kind as RailKind,
      open: p.open,
      dock: p.dock,
      pinned: p.pinned,
      x: p.x,
      y: p.y,
      w: p.w,
      h: p.h,
    }));
  try {
    localStorage.setItem(KEY, JSON.stringify({ panels: open, rails }));
  } catch {
    // Quota / private-mode — persistence is best-effort.
  }
}

let installed = false;

/** Hydrate the saved workspace (open panels + rails) from storage, then keep
 * storage in sync. Idempotent. Rails are hydrated BEFORE `CockpitStage` calls
 * `ensureRails`, so a hidden/moved/pinned rail is restored rather than
 * re-seeded. */
export function installLayoutPersistence(): void {
  if (installed) return;
  installed = true;
  const saved = readLayout();
  const store = usePanelStore.getState();
  store.hydratePanels(saved.panels);
  store.hydrateRails(saved.rails);
  let timer: ReturnType<typeof setTimeout> | undefined;
  usePanelStore.subscribe((state) => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => saveLayout(state.panels), 400);
  });
}
