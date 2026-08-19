export interface SelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

export type SelectTone = "default" | "ok" | "warn" | "bad";

interface SelectProps {
  value: string;
  options: readonly SelectOption[];
  onChange: (value: string) => void;
  /** Names the control for assistive tech — a select with no visible label
   *  still needs one. */
  ariaLabel: string;
  disabled?: boolean;
  /** State of what the value means — a DENY posture is red wherever it is
   *  chosen. Colour belongs to the component, not to each caller. */
  tone?: SelectTone;
  testId?: string;
}

/** The app's one dropdown.
 *
 * Seven surfaces had written their own — schedule rows, the workspace controls,
 * model roles, tools, the approval modal — each re-skinning the native control
 * differently, including the `<option>` popup, which is OS-rendered and needs
 * its own declarations or it comes back system blue.
 *
 * A dropdown rather than a row of cards is the right shape whenever the choice
 * is one-of-many and the options are just names: cards cost a grid of the
 * screen to say what a line of text says.
 */
export function Select({
  value,
  options,
  onChange,
  ariaLabel,
  disabled = false,
  tone = "default",
  testId,
}: SelectProps) {
  return (
    <select
      className={`select${tone === "default" ? "" : ` select--${tone}`}`}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={ariaLabel}
      disabled={disabled}
      data-testid={testId}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value} disabled={o.disabled}>
          {o.label}
        </option>
      ))}
    </select>
  );
}
