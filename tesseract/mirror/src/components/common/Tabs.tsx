import type { ReactNode } from "react";

export interface TabItem<K extends string = string> {
  key: K;
  label: ReactNode;
  /** Headline figure carried on the tab itself — a count, a total, a status
   *  word. Null renders nothing, so a tab can gain a figure without the strip
   *  reflowing between states. */
  badge?: string | number | null;
  testId?: string;
}

interface TabsProps<K extends string> {
  items: readonly TabItem<K>[];
  active: K;
  onSelect: (key: K) => void;
  /** Names the strip for assistive tech — every tablist needs one. */
  label: string;
  /** Tabs share the width equally instead of packing left. For a strip that
   *  spans a fixed panel (Monitor) rather than heading a view. */
  fill?: boolean;
}

/** The app's one horizontal section switcher.
 *
 * Every tabbed surface renders this: the underline, the type, the active
 * colour and the badge treatment are the component's, not the caller's. A
 * surface that wants tabs imports it — the moment one is hand-rolled instead,
 * the app has two answers to the same act and reads as two apps.
 *
 * This is section *switching*. Filter selection is `Chips`; closable document
 * tabs (terminal panes) are their own primitive and deliberately not this.
 */
export function Tabs<K extends string>({
  items,
  active,
  onSelect,
  label,
  fill = false,
}: TabsProps<K>) {
  return (
    <div
      className={`nav-tabs${fill ? " nav-tabs--fill" : ""}`}
      role="tablist"
      aria-label={label}
    >
      {items.map((item) => (
        <button
          key={item.key}
          type="button"
          role="tab"
          aria-selected={item.key === active}
          className={`nav-tab${item.key === active ? " is-active" : ""}`}
          onClick={() => onSelect(item.key)}
          data-testid={item.testId}
        >
          <span className="nav-tab__label">{item.label}</span>
          {item.badge !== undefined && item.badge !== null && (
            <span className="nav-tab__badge">{item.badge}</span>
          )}
        </button>
      ))}
    </div>
  );
}
