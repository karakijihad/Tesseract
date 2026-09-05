/** Icons rendered by more than one surface.
 *
 * A glyph drawn twice is two glyphs the next change has to find. Anything used
 * by a single component stays in that component's file.
 */

/** A circling arrow — "put this back the way it was". */
export function ResetIcon() {
  return (
    <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
      <path
        d="M13 8a5 5 0 1 1-1.6-3.7"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
      />
      <polygon points="13.4,1.8 13.4,5.4 9.8,5.4" fill="currentColor" />
    </svg>
  );
}

/** A short dash — "send this to the dock". Centred rather than sitting on the
 *  baseline, so it lines up with the other title-bar glyphs beside it. */
export function MinimizeIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="1em"
      height="1em"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
    >
      <path d="M6 12 H18" />
    </svg>
  );
}

/** A chevron — "fold this away" pointing down, "unfold it" pointing up. Drawn
 *  on the same centre line as the dash beside it, which the ▾ and ▴ characters
 *  are not: a font puts them where its own metrics say, and the row read as
 *  ragged. */
export function ChevronIcon({ up }: { up: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="1em"
      height="1em"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d={up ? "M6 14.5 L12 9 L18 14.5" : "M6 9.5 L12 15 L18 9.5"} />
    </svg>
  );
}

/** A thumbtack — filled when pinned (held in place), outline when free. */
export function PinIcon({ filled }: { filled: boolean }) {
  return (
    <svg
      viewBox="0 0 24 24"
      width="1em"
      height="1em"
      aria-hidden="true"
      fill={filled ? "currentColor" : "none"}
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinejoin="round"
      strokeLinecap="round"
    >
      <path d="M9 4 H15 L14 9 L16.5 11.5 V13 H7.5 V11.5 L10 9 Z" />
      <path d="M12 13 V20" />
    </svg>
  );
}

/** Single square = maximize; nested squares = restore. */
export function MaximizeIcon({ on }: { on: boolean }) {
  return on ? (
    <svg
      viewBox="0 0 24 24"
      width="1em"
      height="1em"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinejoin="round"
    >
      <rect x="4" y="8" width="12" height="12" rx="1.5" />
      <path d="M8 8 V5.5 A1.5 1.5 0 0 1 9.5 4 H18.5 A1.5 1.5 0 0 1 20 5.5 V14.5 A1.5 1.5 0 0 1 18.5 16 H16" />
    </svg>
  ) : (
    <svg
      viewBox="0 0 24 24"
      width="1em"
      height="1em"
      aria-hidden="true"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinejoin="round"
    >
      <rect x="4.5" y="4.5" width="15" height="15" rx="1.6" />
    </svg>
  );
}
