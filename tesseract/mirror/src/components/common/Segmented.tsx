import type { CSSProperties, ReactNode } from "react";
import { Hint } from "../ui/Hint";

export interface SegmentItem<K extends string | number = string> {
  key: K;
  label: ReactNode;
  /** Hover explanation. Absent renders the segment bare, with no popover. */
  hint?: string;
  /** Required when the label is a glyph rather than a word — a colour swatch
   *  has no readable name of its own. */
  ariaLabel?: string;
  disabled?: boolean;
  /** A custom property the segment paints itself from — the colour swatches
   *  pass their hue. Values, not styling: everything else is the class. */
  style?: CSSProperties;
  testId?: string;
}

interface SegmentedProps<K extends string | number> {
  items: readonly SegmentItem<K>[];
  /** The one that is chosen. */
  value: K;
  onSelect: (key: K) => void;
  /** Names the group for assistive tech — "Security mode", "Text size". */
  label: string;
  /** PLACEMENT ONLY — where the group sits, and how it wraps. */
  className?: string;
}

/** Pick one of a few, laid out side by side.
 *
 * Seven of these existed privately — the security mode, the retention cap,
 * the Telegram override, the cadence and alarm modes, the weekday, the text
 * size and the accent hue. Every one re-derived a radio group by hand, and
 * three of the seven never announced themselves as one: their segments were
 * buttons with a lit class and nothing a screen reader could read as a
 * choice. That is not a thing this component can produce.
 *
 * Distinct from `Chips` by what it means: a chip narrows what a list shows
 * and several can be on at once; a segment IS the setting, and exactly one
 * is.
 */
export function Segmented<K extends string | number>({
  items,
  value,
  onSelect,
  label,
  className,
}: SegmentedProps<K>) {
  return (
    <div
      className={`segmented${className ? ` ${className}` : ""}`}
      role="radiogroup"
      aria-label={label}
    >
      {items.map((item) => (
        <Hint key={item.key} label={item.hint}>
          <button
            type="button"
            role="radio"
            aria-checked={item.key === value}
            aria-label={item.ariaLabel}
            className={`segment${item.key === value ? " is-active" : ""}`}
            style={item.style}
            disabled={item.disabled}
            onClick={() => onSelect(item.key)}
            data-testid={item.testId}
          >
            {item.label}
          </button>
        </Hint>
      ))}
    </div>
  );
}
