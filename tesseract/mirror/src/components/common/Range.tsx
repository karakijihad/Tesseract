/** `hue` renders the accent axis it selects, so the track shows the app's own
 *  palette instead of a bar. Every other slider is `plain`. */
export type RangeTrack = "plain" | "hue";

interface RangeProps {
  value: number;
  onChange: (next: number) => void;
  min: number;
  max: number;
  ariaLabel: string;
  step?: number;
  disabled?: boolean;
  track?: RangeTrack;
  /** Layout only — the width or flex this surface needs. */
  className?: string;
  /** Fires on release (pointer up, touch end, Enter), for a control that
   *  writes to the backend on commit rather than on every pixel of drag. */
  onCommit?: () => void;
  testId?: string;
}

/** The app's one slider.
 *
 * Five surfaces had answered this privately and the disagreement was not about
 * looks — three of them set `accent-color` themselves and two forgot, so two
 * sliders in Settings rendered in the browser's blue on a near-black panel.
 * Two more reached for a bare `<input type="range">` with no aria-label at all.
 *
 * The value is a NUMBER here. Every caller was parsing `e.target.value` back
 * out of a string, and they did not agree on how — `Number`, `parseInt` with a
 * radix, `parseFloat` — for what is one control.
 */
export function Range({
  value,
  onChange,
  min,
  max,
  ariaLabel,
  step,
  disabled = false,
  track = "plain",
  className,
  onCommit,
  testId,
}: RangeProps) {
  return (
    <input
      type="range"
      className={`range${track === "plain" ? "" : ` range--${track}`}${
        className ? ` ${className}` : ""
      }`}
      min={min}
      max={max}
      step={step}
      value={value}
      disabled={disabled}
      aria-label={ariaLabel}
      data-testid={testId}
      onChange={(e) => onChange(Number(e.target.value))}
      onMouseUp={onCommit}
      onTouchEnd={onCommit}
      onKeyUp={(e) => {
        if (e.key === "Enter") onCommit?.();
      }}
    />
  );
}
