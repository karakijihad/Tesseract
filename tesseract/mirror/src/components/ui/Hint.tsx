import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

interface HintProps {
  label: string;
  children: ReactNode;
  /** Where the popover renders relative to the trigger. Defaults to 'top'.
   *  'right' serves the HUD section stacks (2026-07-31) — vertical icon
   *  columns whose labels fly out to the right of each item. */
  position?: "top" | "bottom" | "right";
  /** Maximum popover width — long-form hints (e.g. tag descriptions) need
   *  more room than icon hovers. Default 280px. */
  maxWidth?: number;
}

const VIEWPORT_PAD = 8;
const TRIGGER_GAP = 6;

// Hover/focus popover styled with the brand `--hint-bg` + `--hint-border`
// tokens. Portals to <body> so popovers escape ancestor `overflow: hidden/auto`
// (otherwise they get clipped by scroll panes — see audit note for SCHEDULE pane).
export function Hint({
  label,
  children,
  position = "top",
  maxWidth = 280,
}: HintProps) {
  const triggerRef = useRef<HTMLSpanElement | null>(null);
  const popRef = useRef<HTMLSpanElement | null>(null);
  const [open, setOpen] = useState(false);
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(
    null,
  );

  const reposition = useCallback(() => {
    const trigger = triggerRef.current;
    const pop = popRef.current;
    if (!trigger || !pop) return;
    const tRect = trigger.getBoundingClientRect();
    const pRect = pop.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    let top: number;
    let left: number;
    if (position === "right") {
      top = tRect.top + tRect.height / 2 - pRect.height / 2;
      left = tRect.right + TRIGGER_GAP;
      // Flip left when the label would run off-screen.
      if (left + pRect.width > vw - VIEWPORT_PAD) {
        left = tRect.left - pRect.width - TRIGGER_GAP;
      }
    } else {
      top =
        position === "top"
          ? tRect.top - pRect.height - TRIGGER_GAP
          : tRect.bottom + TRIGGER_GAP;
      left = tRect.left + tRect.width / 2 - pRect.width / 2;

      // Flip if no room above; flip back down if no room below either.
      if (position === "top" && top < VIEWPORT_PAD) {
        top = tRect.bottom + TRIGGER_GAP;
      } else if (
        position === "bottom" &&
        top + pRect.height > vh - VIEWPORT_PAD
      ) {
        top = tRect.top - pRect.height - TRIGGER_GAP;
      }
    }
    // Clamp horizontally.
    left = Math.max(
      VIEWPORT_PAD,
      Math.min(left, vw - pRect.width - VIEWPORT_PAD),
    );
    top = Math.max(
      VIEWPORT_PAD,
      Math.min(top, vh - pRect.height - VIEWPORT_PAD),
    );

    setCoords({ top, left });
  }, [position]);

  useLayoutEffect(() => {
    if (!open) return;
    reposition();
  }, [open, label, reposition]);

  useEffect(() => {
    if (!open) return;
    const handler = () => reposition();
    window.addEventListener("scroll", handler, true);
    window.addEventListener("resize", handler);
    return () => {
      window.removeEventListener("scroll", handler, true);
      window.removeEventListener("resize", handler);
    };
  }, [open, reposition]);

  const handleEnter = () => setOpen(true);
  const handleLeave = () => setOpen(false);

  return (
    <span
      ref={triggerRef}
      className="hint-wrap"
      onMouseEnter={handleEnter}
      onMouseLeave={handleLeave}
      onFocus={handleEnter}
      onBlur={handleLeave}
    >
      {children}
      {open &&
        createPortal(
          <span
            ref={popRef}
            className="hint-pop"
            role="tooltip"
            style={{
              maxWidth,
              top: coords?.top ?? -9999,
              left: coords?.left ?? -9999,
              opacity: coords ? 1 : 0,
            }}
          >
            {label}
          </span>,
          document.body,
        )}
    </span>
  );
}
