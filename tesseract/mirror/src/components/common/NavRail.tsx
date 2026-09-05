import { useMemo, useState, type ReactNode } from "react";
import { Hint } from "../ui/Hint";
import { IconButton } from "./IconButton";

/** How the section behind a rail row is doing.
 *
 * `ok`, `warn` and `bad` are the app's own severities. `quiet` is a section
 * with nothing to report. `unwired` is not a health state at all: it says
 * nothing produces this yet, and it exists because a row that renders quiet
 * when its producer is missing answers a question it cannot answer.
 */
export type NavRailMark = "ok" | "warn" | "bad" | "quiet" | "unwired";

export interface NavRailItem<K extends string = string> {
  key: K;
  label: string;
  icon?: ReactNode;
  /** What is in this section, in one line. The rail becomes the overview
   *  rather than a menu: eight rows, eight sentences, one glance. The words
   *  are the backend's; nothing here writes one. */
  said?: ReactNode;
  /** Drawn as the row's leading edge, and as the whole row when the rail is
   *  folded. */
  mark?: NavRailMark;
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
  /** Lets the rail fold to a strip of marks, for a screen where the pane
   *  needs the width. A thirteen inch screen is a real screen. */
  collapsible?: boolean;
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
  collapsible = false,
}: NavRailProps<K>) {
  const [query, setQuery] = useState("");
  const [folded, setFolded] = useState(false);
  const needle = query.trim().toLowerCase();

  const shown = useMemo(() => {
    // A folded rail has no field to see the query in, so a rail folded while
    // one was typed would hide rows with nothing on screen saying why.
    if (!needle || folded) return groups;
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
  }, [groups, needle, folded]);

  // A rail whose rows carry a line needs the width to carry it, and the rails
  // that do not stay the width they were built for.
  const says = groups.some((g) => (g.items ?? []).some((i) => i.said !== undefined));

  return (
    <aside
      className={`nav-rail${says ? " nav-rail--said" : ""}${
        folded ? " is-folded" : ""
      }`}
      aria-label={label}
    >
      {collapsible && (
        <div className="nav-rail__fold">
          <IconButton
            onClick={() => setFolded((f) => !f)}
            ariaLabel={folded ? `Open ${label.toLowerCase()}` : `Fold ${label.toLowerCase()}`}
            active={folded}
            testId="nav-rail-fold"
          >
            {folded ? "›" : "‹"}
          </IconButton>
        </div>
      )}
      {searchable && !folded && (
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
                }${item.said !== undefined ? " nav-rail__row--said" : ""}${
                  item.mark ? ` nav-rail__row--${item.mark}` : ""
                }`}
                onClick={() => onSelect(item.key)}
                aria-current={item.key === active ? "page" : undefined}
                aria-label={folded ? item.label : undefined}
              >
                {item.mark && (
                  <span className="nav-rail__mark" aria-hidden="true" />
                )}
                {item.icon && (
                  <span className="nav-rail__icon">{item.icon}</span>
                )}
                <span className="nav-rail__lines">
                  <span className="nav-rail__label">{item.label}</span>
                  {item.said !== undefined && (
                    <span className="nav-rail__said">{item.said}</span>
                  )}
                </span>
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
