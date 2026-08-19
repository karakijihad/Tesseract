import { useMemo, useState, type ReactNode } from "react";
import { Hint } from "../ui/Hint";

export interface NavRailItem<K extends string = string> {
  key: K;
  label: string;
  icon?: ReactNode;
}

export interface NavRailFilter {
  key: string;
  label: string;
  /** How many rows this filter would leave on screen. */
  count?: number | null;
  checked: boolean;
  /** What the filter actually selects, when the label is a short tag. */
  hint?: string;
}

export interface NavRailGroup<K extends string = string> {
  label: string;
  /** Rows that switch the pane. */
  items?: NavRailItem<K>[];
  /** Rows that narrow what the pane shows — a checklist, ticked in place, so
   *  the result is visible beside the control rather than one section away.
   *  Operator direction, 2026-08-14. */
  filters?: NavRailFilter[];
  onToggleFilter?: (key: string) => void;
  /** Rendered when any filter in the group is ticked. */
  onResetFilters?: () => void;
}

interface NavRailProps<K extends string> {
  groups: readonly NavRailGroup<K>[];
  active: K;
  onSelect: (key: K) => void;
  /** Names the rail for assistive tech. */
  label: string;
  /** Adds the filter field at the head of the rail. For rails long enough
   *  that reading every row is slower than typing three letters. */
  searchable?: boolean;
  /** Standing note under the rail — what is true of every section, said once
   *  rather than repeated in each. */
  foot?: ReactNode;
}

/** The app's one vertical section switcher — `Tabs` for a list too long to be
 *  a row.
 *
 * Grouped rather than flat, because fourteen equal rows is the infinite column
 * turned on its side. Selection is single by construction: the pane shows one
 * section and never reflows under the pointer while another opens.
 *
 * Filtering narrows the rail only. The open section stays open while you type,
 * so a query that matches nothing cannot blank the pane you were reading.
 */
export function NavRail<K extends string>({
  groups,
  active,
  onSelect,
  label,
  searchable = false,
  foot,
}: NavRailProps<K>) {
  const [query, setQuery] = useState("");
  const needle = query.trim().toLowerCase();

  const shown = useMemo(() => {
    if (!needle) return groups;
    return groups
      .map((g) => ({
        ...g,
        items: (g.items ?? []).filter((i) =>
          i.label.toLowerCase().includes(needle),
        ),
        filters: (g.filters ?? []).filter((f) =>
          f.label.toLowerCase().includes(needle),
        ),
      }))
      .filter((g) => g.items.length > 0 || g.filters.length > 0);
  }, [groups, needle]);

  return (
    <aside className="nav-rail" aria-label={label}>
      {searchable && (
        <div className="nav-rail__search">
          <SearchGlyph />
          <input
            type="search"
            className="nav-rail__input"
            placeholder="Search"
            aria-label={`Search ${label.toLowerCase()}`}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
      )}
      <nav className="nav-rail__nav">
        {shown.map((group) => (
          <div key={group.label} className="nav-rail__group">
            {/* An eyebrow over the rows it heads — `t-label` is the voice,
                `t-meta` the size. It used to get the uppercase for free from
                the tier, which is exactly what stopped hints and paths from
                reading as prose. */}
            <div className="nav-rail__group-label t-meta t-label">{group.label}</div>
            {(group.items ?? []).map((item) => (
              <button
                key={item.key}
                type="button"
                className={`nav-rail__row${
                  item.key === active ? " is-active" : ""
                }`}
                onClick={() => onSelect(item.key)}
                aria-current={item.key === active ? "page" : undefined}
              >
                {item.icon && (
                  <span className="nav-rail__icon">{item.icon}</span>
                )}
                <span className="nav-rail__label">{item.label}</span>
              </button>
            ))}
            {(group.filters ?? []).map((f) => {
              const row = (
                <button
                  type="button"
                  className={`nav-rail__row nav-rail__row--filter${
                    f.checked ? " is-checked" : ""
                  }`}
                  onClick={() => group.onToggleFilter?.(f.key)}
                  aria-pressed={f.checked}
                >
                  <span className="nav-rail__tick" aria-hidden="true">
                    {f.checked ? "✓" : ""}
                  </span>
                  <span className="nav-rail__label">{f.label}</span>
                  {f.count !== undefined && f.count !== null && (
                    <span className="nav-rail__count t-meta">{f.count}</span>
                  )}
                </button>
              );
              return f.hint ? (
                <Hint key={f.key} label={f.hint} position="right" maxWidth={280}>
                  {row}
                </Hint>
              ) : (
                <span key={f.key} className="nav-rail__row-wrap">
                  {row}
                </span>
              );
            })}
            {group.onResetFilters &&
              (group.filters ?? []).some((f) => !f.checked) && (
                <button
                  type="button"
                  className="nav-rail__row nav-rail__row--reset t-meta"
                  onClick={group.onResetFilters}
                >
                  <span className="nav-rail__tick" aria-hidden="true" />
                  <span className="nav-rail__label">Select all</span>
                </button>
              )}
          </div>
        ))}
        {shown.length === 0 && (
          <div className="nav-rail__empty t-meta">
            Nothing matches “{query.trim()}”
          </div>
        )}
      </nav>
      {foot && <div className="nav-rail__foot t-meta">{foot}</div>}
    </aside>
  );
}

function SearchGlyph() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.3"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <circle cx="7.1" cy="7.1" r="4.6" />
      <path d="m10.6 10.6 3 3" />
    </svg>
  );
}
