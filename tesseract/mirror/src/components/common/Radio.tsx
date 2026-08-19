import type { ReactNode } from "react";

interface RadioProps {
  /** The group this option belongs to — the browser's own exclusivity. */
  name: string;
  checked: boolean;
  onChange: () => void;
  label: ReactNode;
  /** What choosing it means, in the column beside the label. */
  hint?: ReactNode;
  disabled?: boolean;
}

/** One option in a set where exactly one holds.
 *
 * A NATIVE input, tinted by `accent-color`, for the reason `Checkbox` is one:
 * arrow-key traversal within the group and the announcement a screen reader
 * makes are the platform's, and rebuilding either out of divs loses them.
 *
 * The label and the hint are columns of the control rather than the surface's,
 * because a set of options that do not line up is not a set. `Segmented` is
 * the other shape of this choice — reach for it when the options are short
 * enough to read as a strip and none of them needs a sentence.
 */
export function Radio({
  name,
  checked,
  onChange,
  label,
  hint,
  disabled = false,
}: RadioProps) {
  return (
    <label className={`radio${disabled ? " is-disabled" : ""}`}>
      <input
        type="radio"
        className="radio__box"
        name={name}
        checked={checked}
        disabled={disabled}
        onChange={onChange}
      />
      <span className="radio__label">{label}</span>
      {hint !== undefined && <span className="radio__hint t-meta">{hint}</span>}
    </label>
  );
}
