import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

/** A menu that opens off a HUD control.
 *
 * The HUD clips its own overflow, so a menu rendered inside it is cut off at
 * the bar's edge. Every one of these therefore portals to `<body>` and
 * positions in viewport coordinates — the same constraint `Hint` works
 * around, and the reason this is a component rather than a convention: the
 * second surface to need it would otherwise re-derive the clipping, the
 * outside-click teardown, the Escape key, and the reposition-on-scroll, and
 * get one of them subtly wrong.
 *
 * `anchor` renders the trigger and is handed the state it needs to describe
 * itself. The caller owns the button's look; this owns where the menu lands
 * and when it closes.
 */
export interface HudMenuProps {
  /** The trigger. `open` drives its pressed styling and aria-expanded. */
  anchor: (state: {
    ref: React.RefObject<HTMLButtonElement | null>;
    open: boolean;
    toggle: () => void;
  }) => ReactNode;
  children: ReactNode;
  /** Names the menu for assistive tech — a portalled popup has no
   *  surrounding context to infer it from. */
  ariaLabel: string;
  /** Which edge of the trigger the menu grows from. The bottom HUD opens
   *  upward, the top bar downward; anything else lands off-screen. */
  side?: "top" | "bottom";
  className?: string;
}

/** Gap between the trigger and the menu, and the viewport margin the menu
 *  will not be pushed past. */
const GAP = 8;
const PAD = 8;

export function HudMenu({
  anchor,
  children,
  ariaLabel,
  side = "bottom",
  className,
}: HudMenuProps) {
  const [open, setOpen] = useState(false);
  const btnRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(
    null,
  );

  const reposition = useCallback(() => {
    const btn = btnRef.current;
    const menu = menuRef.current;
    if (!btn || !menu) return;
    const b = btn.getBoundingClientRect();
    const m = menu.getBoundingClientRect();
    // Centred on the trigger, then clamped so a menu wider than the space
    // beside it slides inward rather than off the edge.
    const left = Math.max(
      PAD,
      Math.min(
        b.left + b.width / 2 - m.width / 2,
        window.innerWidth - m.width - PAD,
      ),
    );
    const top = side === "top" ? b.top - m.height - GAP : b.bottom + GAP;
    setCoords({ top, left });
  }, [side]);

  useLayoutEffect(() => {
    if (open) reposition();
    else setCoords(null);
  }, [open, reposition]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node;
      if (btnRef.current?.contains(t) || menuRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    // Capture phase on scroll: the HUD sits above scrollable panes, and a
    // menu that stayed put while its trigger moved would point at nothing.
    const onMove = () => reposition();
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey);
    window.addEventListener("resize", onMove);
    window.addEventListener("scroll", onMove, true);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", onMove);
      window.removeEventListener("scroll", onMove, true);
    };
  }, [open, reposition]);

  const close = useCallback(() => setOpen(false), []);

  return (
    <>
      {anchor({ ref: btnRef, open, toggle: () => setOpen((v) => !v) })}
      {open &&
        createPortal(
          // brand-exempt: not a control — the menu CONTAINER, which closes
          // once any row inside it has been chosen. The rows are `MenuItem`s
          // and each carries its own action; this only listens for one having
          // happened.
          <div
            ref={menuRef}
            className={`hud-menu${className ? ` ${className}` : ""}`}
            role="menu"
            aria-label={ariaLabel}
            // Rendered off-screen for one frame while it is measured, then
            // faded in at the right place — measuring needs it in the DOM,
            // and showing it mid-measurement is a visible jump.
            style={{
              top: coords?.top ?? -9999,
              left: coords?.left ?? -9999,
              opacity: coords ? 1 : 0,
            }}
            onClick={close}
          >
            {children}
          </div>,
          document.body,
        )}
    </>
  );
}
