interface SwitchProps {
  /** On or off. Carries `aria-pressed`, and moves the knob. */
  on: boolean;
  onToggle: () => void;
  /** Required — the track has no text of its own, so this is the only name
   *  it has. Say what it switches, not that it is a switch. */
  ariaLabel: string;
  disabled?: boolean;
  testId?: string;
}

/** A thing that is on or off, flipped in place.
 *
 * The app's other on/off control is `Checkbox`, and the difference is what
 * the answer does: a checkbox is part of a form you then submit, a switch
 * takes effect the moment it moves. The schedule rows are the second kind —
 * the knob sliding IS the job being disabled.
 */
export function Switch({
  on,
  onToggle,
  ariaLabel,
  disabled = false,
  testId,
}: SwitchProps) {
  return (
    <button
      type="button"
      className={`switch${on ? " is-on" : ""}`}
      onClick={onToggle}
      disabled={disabled}
      aria-pressed={on}
      aria-label={ariaLabel}
      data-testid={testId}
    >
      <span className="switch__knob" aria-hidden="true" />
    </button>
  );
}
