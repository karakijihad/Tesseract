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
