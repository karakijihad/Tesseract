import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";

/** `li` inside a list, `div` anywhere else. The tag is the caller's because
 *  the list semantics are the caller's; the activation contract is not. */
type RowTag = "div" | "li";

interface RowProps {
  children: ReactNode;
  onClick: () => void;
  as?: RowTag;
  disabled?: boolean;
  /** PLACEMENT and the surface's own SHAPE — a bordered card, a bare line, a
   *  tab. What a row is made of differs by surface and always will; how it
   *  activates does not, and that half lives here. Which shapes exist is
   *  declared in `ROW_CLASSES` in `audit-hardcoded-tokens.mjs`, so a fourth
   *  one has to be named there rather than appearing quietly. */
  className?: string;
  ariaLabel?: string;
  /** For a row that opens a detail under itself rather than navigating. */
  ariaExpanded?: boolean;
  testId?: string;
}

/** An activatable surface that contains its own controls.
 *
 * `MenuItem` is the row you can click and nothing else — it is a `<button>`,
 * so a button inside it is invalid HTML. Six surfaces needed the other kind
 * anyway: the four autonomy panes hang Boost / Snooze / Cancel off every row,
 * the terminal tab carries a close, the activity map a dismiss. All six built
 * it by hand, and they disagreed on whether it was reachable at all — the
 * autonomy rows shipped `role`, `tabIndex` and an Enter/Space handler; the
 * terminal tabs shipped none of the three and could not be reached by keyboard.
 *
 * Activation only fires from the row ITSELF. Enter on an inner button bubbles,
 * so the hand-rolled version opened the detail modal every time the operator
 * confirmed a Cancel from the keyboard.
 */
export function Row({
  children,
  onClick,
  as = "div",
  disabled = false,
  className,
  ariaLabel,
  ariaExpanded,
  testId,
}: RowProps) {
  const Tag = as;
  const activate = (e: ReactKeyboardEvent) => {
    // A keypress that started on an inner control belongs to that control.
    if (e.target !== e.currentTarget) return;
    if (e.key !== "Enter" && e.key !== " ") return;
    e.preventDefault();
    if (!disabled) onClick();
  };
  return (
    <Tag
      role="button"
      tabIndex={disabled ? -1 : 0}
      aria-disabled={disabled || undefined}
      aria-label={ariaLabel}
      aria-expanded={ariaExpanded}
      className={`row${className ? ` ${className}` : ""}`}
      onClick={disabled ? undefined : onClick}
      onKeyDown={activate}
      data-testid={testId}
    >
      {children}
    </Tag>
  );
}

interface RowActionsProps {
  children: ReactNode;
  /** PLACEMENT only. */
  className?: string;
}

/** The controls a `Row` carries, and the click they must not hand upward.
 *
 * Every surface that had one wrote the same `onClick={(e) => e.stopPropagation()}`
 * wrapper — six of them, three under a different class name each time. Pressing
 * Cancel is not pressing the row.
 */
export function RowActions({ children, className }: RowActionsProps) {
  return (
    <div
      className={`row__actions${className ? ` ${className}` : ""}`}
      onClick={(e) => e.stopPropagation()}
    >
      {children}
    </div>
  );
}
