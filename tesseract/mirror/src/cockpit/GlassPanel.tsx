// SC-2 + panel-polish — one summoned view as a frosted-glass panel over the
// orb. Drag by the header, resize from any edge/corner (8 handles), pin (lock in
// place — no move/resize; available on every panel incl. the rails), maximize
// (center + expand to fill the stage container), close. Click anywhere to focus.
// The hosted view is memoized on its kind so the high-frequency geometry updates
// during a drag/resize re-render only this frame, never the view inside it.

import { memo, useMemo, type PointerEvent as ReactPointerEvent } from "react";

import {
  usePanelStore,
  isRailKind,
  type RailKind,
  RAIL_W,
  type PanelState,
} from "./panelStore";
import { VIEW_REGISTRY, VIEW_LABELS } from "./viewRegistry";

const MIN_W = 340;
const MIN_H = 240;
// Floating rails resize down to their dock width, not the view-panel floor,
// so an undocked rail doesn't snap 282→340 on first resize.
const railMinW = (isRail: boolean): number => (isRail ? RAIL_W : MIN_W);

type Dir = "n" | "s" | "e" | "w" | "ne" | "nw" | "se" | "sw";
const HANDLES: Dir[] = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];

// A thumbtack — filled when pinned (locked), outline when free.
function PinIcon({ filled }: { filled: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="1em"
      height="1em"
      aria-hidden="true"
      fill={filled ? "currentColor" : "none"}
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinejoin="round"
      strokeLinecap="round"
    >
      <path d="M9 4 H15 L14 9 L16.5 11.5 V13 H7.5 V11.5 L10 9 Z" />
      <path d="M12 13 V20" />
    </svg>
  );
}

// A short underscore — collapse the panel off-stage (reachable from the HUD).
function ResetIcon() {
  return (
    <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
      <path
        d="M13 8a5 5 0 1 1-1.6-3.7"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
      <polygon points="13.4,1.8 13.4,5.4 9.8,5.4" fill="currentColor" />
    </svg>
  );
}

function MinimizeIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="1em"
      height="1em"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
    >
      <path d="M6 18 H18" />
    </svg>
  );
}

// Single square = maximize; nested squares = restore.
function MaximizeIcon({ on }: { on: boolean }) {
  return on ? (
    <svg
      viewBox="0 0 24 24"
      width="1em"
      height="1em"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinejoin="round"
    >
      <rect x="4" y="8" width="12" height="12" rx="1.5" />
      <path d="M8 8 V5.5 A1.5 1.5 0 0 1 9.5 4 H18.5 A1.5 1.5 0 0 1 20 5.5 V14.5 A1.5 1.5 0 0 1 18.5 16 H16" />
    </svg>
  ) : (
    <svg
      viewBox="0 0 24 24"
      width="1em"
      height="1em"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinejoin="round"
    >
      <rect x="4.5" y="4.5" width="15" height="15" rx="1.6" />
    </svg>
  );
}

interface GlassPanelProps {
  panel: PanelState;
  // Geometry a maximized panel fills (stage minus open docked rails). Computed
  // by PanelHost so it stays responsive to viewport + rail changes.
  maximizeRect: { x: number; y: number; w: number; h: number } | null;
  // The visible container size — drag/resize are hard-clamped inside it so a
  // panel can never be dragged off-screen and lost (the container also masks
  // overflow via `.cockpit-center { overflow: hidden }`).
  bounds: { w: number; h: number } | null;
}

function GlassPanelImpl({ panel, maximizeRect, bounds }: GlassPanelProps) {
  const focus = usePanelStore((s) => s.focus);
  const closePanel = usePanelStore((s) => s.closePanel);
  const move = usePanelStore((s) => s.move);
  const place = usePanelStore((s) => s.place);
  const togglePin = usePanelStore((s) => s.togglePin);
  const toggleMaximize = usePanelStore((s) => s.toggleMaximize);
  const toggleMinimize = usePanelStore((s) => s.toggleMinimize);
  const resetRail = usePanelStore((s) => s.resetRail);

  const content = useMemo(() => VIEW_REGISTRY[panel.kind](), [panel.kind]);
  const label = VIEW_LABELS[panel.kind];
  const isRail = isRailKind(panel.kind);
  const maximized = panel.maximized && maximizeRect !== null;
  const geom = maximized ? maximizeRect : panel;
  // No resize handles when maximized, when pinned (locked in place), or on a
  // docked rail (fixed dock width — drag it off to undock, then it resizes).
  const showHandles =
    !maximized && !panel.pinned && !(isRail && panel.dock !== null);

  const startDrag = (e: ReactPointerEvent) => {
    // Locked (pinned) or maximized panels don't move.
    if (maximized || panel.pinned) return;
    e.preventDefault();
    focus(panel.id);
    const ox = e.clientX;
    const oy = e.clientY;
    const bx = panel.x;
    const by = panel.y;
    const onMove = (ev: PointerEvent) => {
      // Hard-clamp inside the visible container — a panel can't be dragged off.
      // Read the panel's live size from the store (not the render-time snapshot)
      // so the clamp bound is correct even if dimensions changed mid-interaction.
      const live =
        usePanelStore.getState().panels.find((p) => p.id === panel.id) ?? panel;
      const maxX = bounds
        ? Math.max(0, bounds.w - live.w)
        : Number.POSITIVE_INFINITY;
      const maxY = bounds
        ? Math.max(0, bounds.h - live.h)
        : Number.POSITIVE_INFINITY;
      const nx = Math.min(Math.max(0, bx + (ev.clientX - ox)), maxX);
      const ny = Math.min(Math.max(0, by + (ev.clientY - oy)), maxY);
      move(panel.id, nx, ny);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  const startResize = (dir: Dir) => (e: ReactPointerEvent) => {
    if (maximized) return;
    e.preventDefault();
    e.stopPropagation();
    focus(panel.id);
    const left = dir.includes("w");
    const right = dir.includes("e");
    const top = dir.includes("n");
    const bottom = dir.includes("s");
    const ox = e.clientX;
    const oy = e.clientY;
    const bx = panel.x;
    const by = panel.y;
    const bw = panel.w;
    const bh = panel.h;
    const minW = railMinW(isRail);
    const onMove = (ev: PointerEvent) => {
      const dx = ev.clientX - ox;
      const dy = ev.clientY - oy;
      let nx = bx;
      let ny = by;
      let nw = bw;
      let nh = bh;
      if (right) nw = Math.max(minW, bw + dx);
      if (bottom) nh = Math.max(MIN_H, bh + dy);
      if (left) {
        nw = Math.max(minW, bw - dx);
        nx = bx + (bw - nw); // keep the right edge fixed
      }
      if (top) {
        nh = Math.max(MIN_H, bh - dy);
        ny = by + (bh - nh); // keep the bottom edge fixed
      }
      nx = Math.max(0, nx);
      ny = Math.max(0, ny);
      // Keep the panel inside the visible container (don't grow past an edge).
      if (bounds) {
        nw = Math.max(minW, Math.min(nw, bounds.w - nx));
        nh = Math.max(MIN_H, Math.min(nh, bounds.h - ny));
      }
      // `place` updates geometry without undocking (drag is what undocks a rail).
      place(panel.id, { x: nx, y: ny, w: nw, h: nh });
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  const stop = (e: ReactPointerEvent) => e.stopPropagation();

  return (
    <div
      className={`glass-panel${panel.open && !panel.minimized ? "" : " glass-panel--hidden"}${maximized ? " glass-panel--maximized" : ""}`}
      style={{
        left: geom.x,
        top: geom.y,
        width: geom.w,
        height: geom.h,
        zIndex: panel.z,
        // Rails dock at RAIL_W, below the generic 340px CSS floor — without
        // this override the rendered box outgrows its computed position and
        // a right-docked rail bleeds past the stage edge.
        minWidth: isRail ? RAIL_W : undefined,
      }}
      data-kind={panel.kind}
      onPointerDown={() => focus(panel.id)}
    >
      {/* Double-click the title bar → raise to the front (top of the z-stack). */}
      <div
        className="glass-panel__bar"
        onPointerDown={startDrag}
        onDoubleClick={() => focus(panel.id)}
      >
        <span className="glass-panel__title">{label}</span>
        <div className="glass-panel__actions">
          {/* Pin = lock in place (no move/resize) — on every panel, rails too. */}
          <button
            type="button"
            className={`glass-panel__btn${panel.pinned ? " is-active" : ""}`}
            aria-label={
              panel.pinned ? `Unlock ${label}` : `Pin ${label} in place`
            }
            aria-pressed={panel.pinned}
            onPointerDown={stop}
            onClick={() => togglePin(panel.id)}
          >
            <PinIcon filled={panel.pinned} />
          </button>
          {isRail && (
            <button
              type="button"
              className="glass-panel__btn"
              aria-label={`Reset ${label} to its default position`}
              title={`Reset ${label} to its default position`}
              onPointerDown={stop}
              onClick={() => resetRail(panel.kind as RailKind)}
            >
              <ResetIcon />
            </button>
          )}
          {!isRail && (
            <button
              type="button"
              className="glass-panel__btn"
              aria-label={`Minimize ${label}`}
              onPointerDown={stop}
              onClick={() => toggleMinimize(panel.id)}
            >
              <MinimizeIcon />
            </button>
          )}
          {!isRail && (
            <button
              type="button"
              className={`glass-panel__btn${panel.maximized ? " is-active" : ""}`}
              aria-label={
                panel.maximized ? `Restore ${label}` : `Maximize ${label}`
              }
              aria-pressed={panel.maximized}
              onPointerDown={stop}
              onClick={() => toggleMaximize(panel.id)}
            >
              <MaximizeIcon on={panel.maximized} />
            </button>
          )}
          <button
            type="button"
            className="glass-panel__btn glass-panel__close"
            aria-label={`Close ${label}`}
            onPointerDown={stop}
            onClick={() => closePanel(panel.id)}
          >
            ×
          </button>
        </div>
      </div>
      <div className="glass-panel__body">{content}</div>
      {showHandles &&
        HANDLES.map((dir) => (
          <div
            key={dir}
            className={`glass-panel__resize glass-panel__resize--${dir}`}
            aria-hidden="true"
            onPointerDown={startResize(dir)}
          />
        ))}
    </div>
  );
}

export const GlassPanel = memo(GlassPanelImpl);
