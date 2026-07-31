import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { useHudDockStore } from "../../../stores/hudDock";

// Sectioned dock (2026-07-31) — one HUD section button whose contents open as
// a vertical glass stack above it. The stack portals to <body> with fixed
// coords because `.cockpit-hud` clips overflow (same constraint that drove
// SummonedPanes' portal). One stack open at a time via useHudDockStore;
// outside clicks close it.
interface HudSectionProps {
  id: string;
  label: string;
  icon: ReactNode;
  badge?: number;
  /** Section-face state tint, e.g. observer-green when armed. */
  live?: boolean;
  children: ReactNode;
}

const STACK_GAP = 8;
const STACK_PAD = 8;

function badgeText(n: number): string {
  return n > 9 ? "9+" : String(n);
}

export function HudSection({
  id,
  label,
  icon,
  badge,
  live,
  children,
}: HudSectionProps) {
  const open = useHudDockStore((s) => s.openSection === id);
  const toggleSection = useHudDockStore((s) => s.toggleSection);
  const closeSections = useHudDockStore((s) => s.closeSections);
  const btnRef = useRef<HTMLButtonElement>(null);
  const stackRef = useRef<HTMLDivElement>(null);
  const [coords, setCoords] = useState<{ bottom: number; left: number } | null>(
    null,
  );

  const reposition = useCallback(() => {
    const btn = btnRef.current;
    const stack = stackRef.current;
    if (!btn || !stack) return;
    const bRect = btn.getBoundingClientRect();
    const sRect = stack.getBoundingClientRect();
    const vw = window.innerWidth;
    // Anchored above the button, left-aligned; clamped on-screen so a
    // right-edge section's stack never leaves the viewport.
    const bottom = window.innerHeight - bRect.top + STACK_GAP;
    let left = bRect.left;
    left = Math.max(STACK_PAD, Math.min(left, vw - sRect.width - STACK_PAD));
    setCoords({ bottom, left });
  }, []);

  useLayoutEffect(() => {
    if (open) reposition();
    else setCoords(null);
  }, [open, reposition]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (btnRef.current?.contains(t) || stackRef.current?.contains(t)) return;
      closeSections();
    };
    const onResize = () => reposition();
    window.addEventListener("mousedown", onDown);
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("resize", onResize);
    };
  }, [open, closeSections, reposition]);

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        className={`hud-tab hud-section${open ? " is-open" : ""}${live ? " is-live" : ""}`}
        onClick={() => toggleSection(id)}
        aria-label={label}
        aria-expanded={open}
        aria-haspopup="menu"
      >
        <span className="hud-tab-icon" aria-hidden="true">
          {icon}
        </span>
        {badge !== undefined && badge > 0 && (
          <span className="hud-tab-badge" aria-hidden="true">
            {badgeText(badge)}
          </span>
        )}
      </button>
      {open &&
        createPortal(
          <div
            ref={stackRef}
            className="hud-section-stack"
            role="menu"
            aria-label={label}
            style={{
              bottom: coords?.bottom ?? -9999,
              left: coords?.left ?? -9999,
              opacity: coords ? 1 : 0,
            }}
          >
            {children}
          </div>,
          document.body,
        )}
    </>
  );
}
