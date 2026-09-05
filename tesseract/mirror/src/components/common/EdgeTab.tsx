import type { ReactNode } from "react";

export type EdgeTabSide = "left" | "right" | "bottom";

interface EdgeTabProps {
  children: ReactNode;
  onClick: () => void;
  /** Which edge it clings to — the edge the hidden thing went behind. */
  side: EdgeTabSide;
  /** Required — the tab is a glyph, so this is the only name it has. */
  ariaLabel: string;
  /** Cling to the nearest positioned ancestor's edge instead of the window's,
   *  for a rail that lives inside a panel rather than filling the screen. */
  inset?: boolean;
}

/** A hidden surface's way back, clinging to the edge it went behind.
 *
 * The bottom HUD and the side rails each had their own, and the second was
 * written by copying the first — "same glass, same accent border, same idea",
 * as its own comment says. One idea, one control: it reappears from its own
 * edge rather than from a button parked somewhere else.
 */
export function EdgeTab({ children, onClick, side, ariaLabel, inset = false }: EdgeTabProps) {
  return (
    <button
      type="button"
      className={`edge-tab edge-tab--${side}${inset ? " edge-tab--inset" : ""}`}
      onClick={onClick}
      aria-label={ariaLabel}
    >
      <span aria-hidden="true">{children}</span>
    </button>
  );
}
