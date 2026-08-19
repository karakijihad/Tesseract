import type {
  KeyboardEvent as ReactKeyboardEvent,
  MouseEvent as ReactMouseEvent,
  ReactNode,
} from "react";

export type DisclosureVariant = "text" | "row";

interface DisclosureProps {
  /** The label. It says what opening will show, and callers word it for the
   *  state they are in — "show less", "collapse", "+3 older". */
  children: ReactNode;
  /** Whether the thing this reveals is showing. Carries `aria-expanded`, so
   *  a disclosure that only *looks* open is not a thing this can produce. */
  open: boolean;
  /** The event is passed on for the one case that needs it: a disclosure
   *  inside a card that is itself clickable has to stop the click there. */
  onToggle: (e: ReactMouseEvent) => void;
  /** `text` is an inline show-more inside a body of content; `row` is the
   *  full-width head of a section that opens under it. */
  variant?: DisclosureVariant;
  disabled?: boolean;
  ariaLabel?: string;
  /** The element the disclosure opens, for assistive tech. */
  ariaControls?: string;
  id?: string;
  onKeyDown?: (e: ReactKeyboardEvent<HTMLButtonElement>) => void;
  /** PLACEMENT ONLY — where it sits, or how its own columns are laid out.
   *  `audit:tokens` fails a caller class that paints one. */
  className?: string;
  testId?: string;
}

/** A control that shows more of something, or puts it away again.
 *
 * Nine of these were hand-rolled and no two agreed on what "there is more"
 * looks like: two accent links, an uppercase meta header, a mono toggle, a
 * plain meta line, and four full-width section heads with three different
 * hovers. Half carried `aria-expanded` and half did not, so the same gesture
 * was announced to a screen reader in one place and silent in the next.
 *
 * The caret and the wording stay with the caller — "+3 older" and "collapse"
 * are content. What is shared is the look, the hover, and the promise that
 * the state is always announced.
 */
export function Disclosure({
  children,
  open,
  onToggle,
  variant = "text",
  disabled = false,
  ariaLabel,
  ariaControls,
  id,
  onKeyDown,
  className,
  testId,
}: DisclosureProps) {
  return (
    <button
      type="button"
      id={id}
      className={`disclosure${variant === "row" ? " disclosure--row" : ""}${
        open ? " is-open" : ""
      }${className ? ` ${className}` : ""}`}
      onClick={onToggle}
      onKeyDown={onKeyDown}
      disabled={disabled}
      aria-expanded={open}
      aria-controls={ariaControls}
      aria-label={ariaLabel}
      data-testid={testId}
    >
      {children}
    </button>
  );
}
