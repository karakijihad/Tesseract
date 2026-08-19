interface ColorWellProps {
  /** A `#rrggbb` string — the only form `<input type="color">` accepts. */
  value: string;
  onChange: (next: string) => void;
  ariaLabel: string;
  disabled?: boolean;
  /** Layout only. */
  className?: string;
  testId?: string;
}

/** A colour the operator picks by clicking the colour itself.
 *
 * The swatch IS the control — a preview beside a button is one more thing to
 * find. Browsers wrap the native well in chrome of their own, which is what
 * made it read as a form field borrowed from another app; that chrome is
 * stripped once here rather than by whichever surface notices.
 */
export function ColorWell({
  value,
  onChange,
  ariaLabel,
  disabled = false,
  className,
  testId,
}: ColorWellProps) {
  return (
    <input
      type="color"
      className={className ? `color-well ${className}` : "color-well"}
      value={value}
      disabled={disabled}
      aria-label={ariaLabel}
      data-testid={testId}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}
