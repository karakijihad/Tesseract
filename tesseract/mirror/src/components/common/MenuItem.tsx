import type {
  MouseEvent as ReactMouseEvent,
  PointerEvent as ReactPointerEvent,
  ReactNode,
} from "react";

export type MenuItemRole =
  | "menuitem"
  | "menuitemcheckbox"
  | "menuitemradio"
  | "option";

interface MenuItemProps {
  children: ReactNode;
  onClick: () => void;
  /** The choice that is already the case — the current mode, the open
   *  document, the voice in use. Eight surfaces spelled this `is-current`,
   *  `is-primary`, `is-open` and `is-active`; it is one state. */
  active?: boolean;
  /** Where a keyboard cursor is, when the list is driven from somewhere else
   *  — the slash hint moves through its rows while focus stays in the
   *  composer. Distinct from `:focus-visible`, which the browser owns. */
  focused?: boolean;
  disabled?: boolean;
  /** `menuitemcheckbox`/`menuitemradio` for a row that reports a state, and
   *  then `checked` carries it. Default is a plain action row. */
  role?: MenuItemRole;
  checked?: boolean;
  ariaLabel?: string;
  /** A row inside a draggable card has to stop the pointer before the drag
   *  starts, or a click-to-open fights the card it sits on. */
  onPointerDown?: (e: ReactPointerEvent) => void;
  /** A list whose keyboard cursor follows the pointer moves `focused` from
   *  here rather than from `:hover`. */
  onMouseEnter?: () => void;
  /** A row that must not take focus on its way to a click — the slash hint
   *  is walked while the composer keeps it. */
  onMouseDown?: (e: ReactMouseEvent) => void;
  /** Rename-in-place, on the one list that has it. */
  onDoubleClick?: () => void;
  /** PLACEMENT ONLY — where the row sits in its parent's layout (`flex: 1`,
   *  a negative margin, a grid template). What it LOOKS like belongs here,
   *  and `audit:tokens` fails a caller class that paints one. */
  className?: string;
  testId?: string;
}

/** One choice in a list of them — a row in a popover menu, a dropdown, or a
 *  pane's own list of things to pick.
 *
 * Ten surfaces had their own: the voice picker, the document list, the chat
 * manager, the activity taskbar, the folder card, the slash hint, the shell
 * dropdown, the snooze menu, the assistant menu and the voice-mode menu. They
 * agreed on the shape — transparent, left-aligned, full width — and disagreed
 * on every state: hover was `--bg-hover` in three, `--accent-dim` in two, a
 * border colour in two, `--bg-surface` in one and a raw `color-mix` in the
 * last, so pointing at a row meant something different in each list.
 *
 * The row is chrome only. Columns, icons and trailing meta are children —
 * which is why this takes none of them as props.
 */
export function MenuItem({
  children,
  onClick,
  active = false,
  focused = false,
  disabled = false,
  role = "menuitem",
  checked,
  ariaLabel,
  onPointerDown,
  onMouseEnter,
  onMouseDown,
  onDoubleClick,
  className,
  testId,
}: MenuItemProps) {
  return (
    <button
      type="button"
      role={role}
      className={`menu-item${active ? " is-active" : ""}${
        focused ? " is-focused" : ""
      }${className ? ` ${className}` : ""}`}
      onClick={onClick}
      onPointerDown={onPointerDown}
      onMouseEnter={onMouseEnter}
      onMouseDown={onMouseDown}
      onDoubleClick={onDoubleClick}
      disabled={disabled}
      aria-checked={checked}
      // A listbox option reports selection, not checkedness — and in a list
      // driven by a cursor, the row under the cursor IS the selected one.
      aria-selected={role === "option" ? active || focused : undefined}
      aria-label={ariaLabel}
      data-testid={testId}
    >
      {children}
    </button>
  );
}
