import { forwardRef, type MouseEvent as ReactMouseEvent, type ReactNode } from "react";

export type ButtonTone = "default" | "primary" | "good" | "danger" | "inline";

interface ButtonProps {
  children: ReactNode;
  onClick: () => void;
  /** A control that must not steal focus when clicked — the approval card
   *  keeps the composer focused so `y`/`n` still work after a mouse answer.
   *  `IconButton` carries `onPointerDown` for the same class of reason. */
  onMouseDown?: (e: ReactMouseEvent) => void;
  disabled?: boolean;
  /** Pressed state for a toggle — carries `aria-pressed`, so a button that
   *  only *looks* active is not a thing this component can produce. */
  active?: boolean;
  /** `primary` for the one action a pane is FOR — filled, not outlined.
   *  `good` accepts, `danger` stops or destroys. `inline` sits INSIDE a line
   *  of text and takes that line's size and colour, for an action that must
   *  stay subordinate to the sentence carrying it. Default is the quiet
   *  outline every other action wears. */
  tone?: ButtonTone;
  ariaLabel?: string;
  ariaExpanded?: boolean;
  /** `submit` for the one action that belongs to a form rather than to a
   *  handler — the comment box, where Enter should send it too. */
  type?: "button" | "submit";
  testId?: string;
}

/** The app's one small action button.
 *
 * Five views had their own — `agents-view-btn`, `schedule-header-btn`,
 * `pulse-header-btn`, `conscience-view-refresh`, `identity-view-refresh` —
 * differing in font, case, border and hover, all of them saying "refresh".
 * This is the button for a view head, a block head, or a form's own action;
 * the chat composer and the cockpit HUD have their own shapes and are
 * deliberately not this, and neither is an icon-only control (`IconButton`).
 *
 * `tone` is where a surface's variant goes. It is the reason `cost-row__save`
 * existed as a filled violet slab of its own and `mcp-approvals` drew a green
 * accept out of a raw rgba: the shape was here, the emphasis was not.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    children,
    onClick,
    onMouseDown,
    disabled = false,
    active,
    tone = "default",
    ariaLabel,
    ariaExpanded,
    type = "button",
    testId,
  },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={`btn${tone === "default" ? "" : ` btn--${tone}`}${
        active ? " is-active" : ""
      }`}
      onClick={onClick}
      onMouseDown={onMouseDown}
      disabled={disabled}
      aria-pressed={active}
      aria-label={ariaLabel}
      aria-expanded={ariaExpanded}
      data-testid={testId}
    >
      {children}
    </button>
  );
});
