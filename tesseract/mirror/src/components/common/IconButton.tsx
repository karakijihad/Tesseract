import type { PointerEvent as ReactPointerEvent, ReactNode } from "react";

interface IconButtonProps {
  /** An icon or a single glyph. Text belongs in `Button`. */
  children: ReactNode;
  onClick: () => void;
  /** Required — an icon with no accessible name is a button nobody can read. */
  ariaLabel: string;
  /** Pressed state for a toggle: carries `aria-pressed` and the lit treatment. */
  active?: boolean;
  disabled?: boolean;
  /** Title bars drag from their background, so a button inside one has to stop
   *  the pointer before the drag starts. */
  onPointerDown?: (e: ReactPointerEvent) => void;
  testId?: string;
}

/** The app's one icon button — the pin, reset, minimise, maximise and close of
 *  every title bar.
 *
 * Glass panels, canvas surfaces and the Routing map each had their own, at
 * three sizes and two glyph scales, so the same × was a different button
 * depending on which card it sat in.
 */
export function IconButton({
  children,
  onClick,
  ariaLabel,
  active,
  disabled = false,
  onPointerDown,
  testId,
}: IconButtonProps) {
  return (
    <button
      type="button"
      className={`icon-btn${active ? " is-active" : ""}`}
      onClick={onClick}
      onPointerDown={onPointerDown}
      aria-label={ariaLabel}
      aria-pressed={active}
      disabled={disabled}
      data-testid={testId}
    >
      {children}
    </button>
  );
}
