export type ScrimLevel = "panel" | "drawer" | "dialog";

interface ScrimProps {
  onClick: () => void;
  /** Required — the scrim is a button, and a button whose only job is
   *  "dismiss this" still has to say what it dismisses. */
  ariaLabel: string;
  /** What the scrim sits under. The three surfaces that need one stack in a
   *  fixed order — an expanded preview, a drawer, a modal — so the level is a
   *  name here and a z-index in one place, rather than three files each
   *  picking a number and hoping. */
  level?: ScrimLevel;
}

/** The full-viewport click-catcher behind an overlay.
 *
 * Three surfaces hand-rolled the same fixed/inset-0/border-0/cursor-pointer
 * button — the expand overlay, the session drawer and the reset dialog — and
 * two of them dimmed the app to a different depth. It is one darkness now:
 * whichever surface is open, what is behind it looks equally far away.
 */
export function Scrim({ onClick, ariaLabel, level = "dialog" }: ScrimProps) {
  return (
    <button
      type="button"
      className={`scrim scrim--${level}`}
      onClick={onClick}
      aria-label={ariaLabel}
    />
  );
}
