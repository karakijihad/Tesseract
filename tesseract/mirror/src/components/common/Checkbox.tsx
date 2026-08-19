import type { ReactNode, Ref } from "react";

/** `state` tints the label the way a status reads — green for on, dim for
 *  off. A LIBRARY feature rather than a per-surface one: the notifications
 *  pane had this privately and lost it on the way in, and the next surface
 *  that wants it would otherwise write the same two rules again. */
export type CheckboxTone = "plain" | "state";

interface Labelled {
  /** Text beside the box. */
  label: ReactNode;
  ariaLabel?: never;
}

interface Bare {
  /** No text: a selection column, or a cell whose name sits in the column
   *  beside it. The box is still a control, so it must be named. */
  label?: never;
  ariaLabel: string;
}

type CheckboxProps = (Labelled | Bare) & {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  /** `state` colours the label by `checked`. Default `plain`. */
  tone?: CheckboxTone;
  /** For the indeterminate state — the drawer's select-all sets `.indeterminate`
   *  on the node, which no React prop reaches. */
  inputRef?: Ref<HTMLInputElement>;
  /** When a `<label htmlFor>` elsewhere on the surface names this box. Its own
   *  `label` is the usual answer; this is for a field whose caption belongs to
   *  a row of controls rather than to the box alone. */
  id?: string;
};

/** The app's one checkbox.
 *
 * Six surfaces had answered this privately — four gap values, three text
 * colours, and one that was not a checkbox at all but a `div` drawn to look
 * like one, with its own on/off palette. A box that is 6px from its label in
 * Settings and 8px away in Identity is the same defect as a second hint
 * colour: small enough to survive every review, and the reason the app read
 * as several applications.
 *
 * A NATIVE input, tinted by `accent-color`. The fake-box version could not be
 * reached by the keyboard or announced by a screen reader without rebuilding
 * what the platform already ships.
 */
export function Checkbox({
  checked,
  onChange,
  label,
  disabled = false,
  tone = "plain",
  ariaLabel,
  inputRef,
  id,
}: CheckboxProps) {
  const box = (
    <input
      ref={inputRef}
      id={id}
      type="checkbox"
      className="checkbox__box"
      checked={checked}
      disabled={disabled}
      onChange={(e) => onChange(e.target.checked)}
      aria-label={ariaLabel}
    />
  );
  // A bare box is the control itself; wrapping it in a <label> with no text
  // would put an empty clickable band beside every one of them.
  if (label === undefined) return box;
  const state = tone === "state" ? (checked ? " is-on" : " is-off") : "";
  return (
    <label className={`checkbox${disabled ? " is-disabled" : ""}${state}`}>
      {box}
      <span className="checkbox__label">{label}</span>
    </label>
  );
}
