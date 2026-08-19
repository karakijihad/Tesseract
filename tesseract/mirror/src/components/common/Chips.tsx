import type { ReactNode } from "react";

export interface ChipItem<K extends string = string> {
  key: K;
  label: ReactNode;
  /** Count of what selecting this chip would leave on screen. */
  count?: number | null;
}

interface ChipsProps<K extends string> {
  items: readonly ChipItem<K>[];
  active: K;
  onSelect: (key: K) => void;
  label: string;
}

/** The app's one filter-selection control — a wrapping row of pills.
 *
 * Distinct from `Tabs` by what it means, not by how it looks: a tab changes
 * which section you are in, a chip narrows what the section shows. Both were
 * hand-rolled three times over before this existed, in three different type
 * sizes.
 */
export function Chips<K extends string>({
  items,
  active,
  onSelect,
  label,
}: ChipsProps<K>) {
  return (
    <div className="nav-chips" role="tablist" aria-label={label}>
      {items.map((item) => (
        <button
          key={item.key}
          type="button"
          role="tab"
          aria-selected={item.key === active}
          className={`nav-chip${item.key === active ? " is-active" : ""}`}
          onClick={() => onSelect(item.key)}
        >
          {item.label}
          {item.count !== undefined && item.count !== null && (
            <span className="nav-chip__count">{item.count}</span>
          )}
        </button>
      ))}
    </div>
  );
}
