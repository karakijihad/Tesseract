import type { MouseEvent as ReactMouseEvent, ReactNode } from "react";

export type ChipTone = "default" | "good" | "warn" | "bad";
export type ChipVariant = "outline" | "tag";

interface ChipProps {
  children: ReactNode;
  /** The event is passed on for the one case that needs it: a chip inside a
   *  card that is itself clickable has to stop the click there. */
  onClick: (e: ReactMouseEvent) => void;
  /** Switched on — a filter that is letting things through, a view that is
   *  the one being shown. Carries `aria-pressed`. */
  active?: boolean;
  disabled?: boolean;
  /** What the chip is reporting, when it reports a state rather than a
   *  choice — the context meter goes `warn` and then `bad`, a model role
   *  reads `good` while it is active. */
  tone?: ChipTone;
  /** `outline` is the bordered token. `tag` is a chip whose colour comes
   *  from what it labels — the pulse categories, which are coloured by
   *  category everywhere else in the app and would lose that under a border
   *  of their own. */
  variant?: ChipVariant;
  ariaLabel?: string;
  ariaExpanded?: boolean;
  ariaHasPopup?: "menu" | "listbox" | "dialog";
  /** PLACEMENT ONLY — where the chip sits, and any layout its own children
   *  need. `audit:tokens` fails a caller class that paints one. */
  className?: string;
  testId?: string;
}

/** A compact token you can click — a filter, a value that opens its editor,
 *  a status that opens its detail.
 *
 * Seven of these were private: the pulse cap, the cadence pill, the path
 * pill, the context meter, the chat trigger, the activity count and the
 * command tips. Radii ran 3px, 10px, 999px and two token values; hover was a
 * border colour in four and a background in two; one dimmed and the rest lit.
 * They are one token now, and `is-active` means the same thing in all of
 * them.
 */
export function Chip({
  children,
  onClick,
  active = false,
  disabled = false,
  tone = "default",
  variant = "outline",
  ariaLabel,
  ariaExpanded,
  ariaHasPopup,
  className,
  testId,
}: ChipProps) {
  return (
    <button
      type="button"
      className={`chip chip--${variant}${
        tone === "default" ? "" : ` chip--${tone}`
      }${active ? " is-active" : ""}${className ? ` ${className}` : ""}`}
      onClick={onClick}
      disabled={disabled}
      aria-pressed={active}
      aria-label={ariaLabel}
      aria-expanded={ariaExpanded}
      aria-haspopup={ariaHasPopup}
      data-testid={testId}
    >
      {children}
    </button>
  );
}
