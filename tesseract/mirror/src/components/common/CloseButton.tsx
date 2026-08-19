import type { PointerEvent as ReactPointerEvent } from "react";

import { IconButton } from "./IconButton";

interface CloseButtonProps {
  onClick: () => void;
  /** What is being closed — "Close pane", "Dismiss suggestion". The glyph
   *  carries no text, so this is the whole accessible name. */
  ariaLabel: string;
  /** `inline` for a dismissal that lives inside a row, a chip or a tab, where
   *  a 1.5rem title-bar button would crowd the row it sits in. */
  size?: "default" | "inline";
  disabled?: boolean;
  /** The close has been asked for and has not come back yet. Renders the wait
   *  glyph and refuses a second click. */
  busy?: boolean;
  /** Title bars drag from their background, so a button inside one has to stop
   *  the pointer before the drag starts. */
  onPointerDown?: (e: ReactPointerEvent) => void;
  testId?: string;
}

/** The app's one close.
 *
 * Nineteen sites rendered a close by hand, in two different characters and
 * twelve private classes, so the same act was a different control depending on
 * which surface it sat in. This owns the glyph, the accessible-name pattern
 * and the hover; the caller supplies only what is being closed.
 *
 * Placement and reveal-on-hover stay with the parent that owns the layout — a
 * pane close is absolutely positioned and a tab close fades in, and neither is
 * a property of the button. Parents wrap it in a slot element of their own
 * rather than reaching in to restyle it.
 */
export function CloseButton({
  onClick,
  ariaLabel,
  size = "default",
  disabled = false,
  busy = false,
  onPointerDown,
  testId,
}: CloseButtonProps) {
  const glyph = busy ? "…" : "×";

  if (size === "default") {
    return (
      <IconButton
        onClick={onClick}
        ariaLabel={ariaLabel}
        disabled={disabled || busy}
        onPointerDown={onPointerDown}
        testId={testId}
      >
        {glyph}
      </IconButton>
    );
  }

  return (
    <button
      type="button"
      className="close-btn-inline"
      onClick={onClick}
      onPointerDown={onPointerDown}
      aria-label={ariaLabel}
      disabled={disabled || busy}
      data-testid={testId}
    >
      {glyph}
    </button>
  );
}
