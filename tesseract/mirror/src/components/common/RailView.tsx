import { useEffect, useRef, useState, type ComponentType, type ReactNode } from "react";

import { useUIStore, type View } from "../../stores/ui";
import { ErrorBoundary } from "./ErrorBoundary";
import {
  NavRail,
  type NavRailFilter,
  type NavRailGroup,
  type NavRailMark,
} from "./NavRail";
import { ViewHeader } from "./ViewHeader";

export interface RailSection<K extends string = string> {
  key: K;
  /** The rail row's text. */
  label: string;
  /** Head text when it should read longer than the rail row ("Cost" in the
   *  rail, "Cost & budgets" above the pane). */
  title?: string;
  icon?: ReactNode;
  /** A section that owns state — its own fetches, its own hooks. Rendered as
   *  an element, so it keeps its identity while the view around it re-renders. */
  Body?: ComponentType;
  /** A section that is a closure over the VIEW's state. Called, not mounted:
   *  an inline `Body` would be a new component type on every render, which
   *  remounts the section and drops its scroll position. Exactly one of the
   *  two is set. */
  render?: () => ReactNode;
  /** This section's state line. A count belongs to what it counts — the head
   *  said "31 enabled · 31 total" over the Alarms section until it did. */
  meta?: ReactNode;
  /** This section's controls. Add and refresh act on what is in the pane, so
   *  they live with it rather than on the view. */
  actions?: ReactNode;
  /** What is in this section, in one line, on the rail row itself. A rail of
   *  names is a menu; a rail of sentences is the overview. */
  said?: ReactNode;
  /** How this section is doing, drawn on its row. */
  mark?: NavRailMark;
}

export interface RailGroup<K extends string = string> {
  label: string;
  /** Rows that switch the pane. */
  sections?: RailSection<K>[];
  /** Rows that narrow what the open section shows. A filter group has no
   *  sections — ticking a row leaves you where you are and changes what is in
   *  front of you, which is the point (operator, 2026-08-14). */
  filters?: NavRailFilter[];
  onToggleFilter?: (key: string) => void;
  onResetFilters?: () => void;
}

interface RailViewProps<K extends string> {
  groups: readonly RailGroup<K>[];
  /** Which panel this rail belongs to. `cockpit_show` names a panel AND
   *  a row, and a rail may only take a row addressed to its own panel:
   *  section keys repeat across panels, so the first rail mounted with a
   *  matching key used to consume a request meant for another view. */
  view: View;
  /** Names the rail for assistive tech — "Settings sections". */
  label: string;
  /** State that is true of the whole view, whichever section is open. A
   *  section's own `meta` wins over it. */
  meta?: ReactNode;
  /** Controls that act on the whole view. A section's own `actions` win. */
  actions?: ReactNode;
  searchable?: boolean;
  foot?: ReactNode;
  /** Section open on first render. Defaults to the first. */
  initial?: K;
  /** Which section is open, when the VIEW owns that. Pair it with
   *  `onSectionChange`. A view needs this when one of its sections links to
   *  another: opening a panel the reader is already inside does nothing, so
   *  the rail has to move instead. Omit it and the rail owns its own
   *  selection, which is what every other caller wants. */
  active?: K;
  /** Lifts selection out when the view needs to know which section is open
   *  (the autonomy view snapshot reports it). */
  onSectionChange?: (key: K) => void;
  /** Lets the rail fold to a strip of marks. */
  collapsible?: boolean;
  /** What frames the pane. `header` is the app's view head and is what every
   *  view wants. `bare` hands the whole pane to the section, for a surface
   *  that carries its own template — the Autonomy panel's rooms are one
   *  shape, built once, and a view head above it would be a second head. It
   *  is not licence to draw one: a section rendering `bare` still gets its
   *  chrome from a component rather than writing one. */
  chrome?: "header" | "bare";
}

/** Calls a `render` section INSIDE the boundary rather than beside it.
 *
 * `section.render?.()` written in RailView's own JSX is an ordinary call
 * evaluated while RailView renders — it runs before the ErrorBoundary element
 * around it is even constructed, so a throw escapes the boundary that appears
 * to wrap it and blanks the window anyway. A `Body` is safe for the mirror
 * image of the same reason: `<Body />` only describes an element, and React
 * invokes it later, under the boundary.
 *
 * Declared at module scope, so it is the same component type on every render
 * and the pane keeps its scroll — an inline one would remount it.
 */
function Rendered({ fn }: { fn: () => ReactNode }) {
  return <>{fn()}</>;
}

/** A view built as a rail of sections and one pane — the app's one shape for a
 *  view with more than one thing in it.
 *
 * Settings proved it and every other view followed (operator direction,
 * 2026-08-14): the sections are up front, one body is mounted at a time, and
 * the pane never reflows under the pointer while another opens. A view that
 * needs sections renders this rather than pairing a rail with a head of its
 * own — which is how the app had nine heads before.
 */
export function RailView<K extends string>({
  groups,
  view,
  label,
  meta,
  actions,
  searchable = false,
  foot,
  initial,
  active: controlled,
  onSectionChange,
  collapsible = false,
  chrome = "header",
}: RailViewProps<K>) {
  const sections = groups.flatMap((g) => g.sections ?? []);
  const [own, setOwn] = useState<K>(initial ?? sections[0].key);
  const active = controlled ?? own;
  const section = sections.find((s) => s.key === active) ?? sections[0];
  const Body = section.Body;

  // A rail whose rows ARE the data (agents, channels) opens on one of them,
  // so the surface that reads the selection has to be told which — otherwise
  // the rail shows a row highlighted and the pane shows "nothing selected".
  const announced = useRef(false);
  useEffect(() => {
    if (announced.current) return;
    announced.current = true;
    onSectionChange?.(active);
    // Mount only: later changes come through `select`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const select = (key: K) => {
    if (controlled === undefined) setOwn(key);
    onSectionChange?.(key);
  };

  // `cockpit_show` names a panel and a row; the panel is a store the app
  // already had, the row is not, so it waits here until the rail that owns
  // that key is mounted. Matched case-insensitively because the assistant is
  // repeating what the operator said out loud, not reading a key off the
  // wire. Consumed once, so it moves one rail once.
  //
  // The panel is checked first. Without it a row is taken by whichever rail
  // has a key of that name, and several do.
  const requested = useUIStore((s) => s.requestedSection);
  const takeRequested = useUIStore((s) => s.takeRequestedSection);
  useEffect(() => {
    if (!requested || requested.view !== view) return;
    const hit = sections.find(
      (s) => s.key.toLowerCase() === requested.section.toLowerCase(),
    );
    if (!hit) return;
    takeRequested();
    select(hit.key);
    // `select` closes over props that are stable for this rail's lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [requested]);

  const railGroups: NavRailGroup<K>[] = groups.map((g) => ({
    label: g.label,
    items: (g.sections ?? []).map(({ key, label: rowLabel, icon, said, mark }) => ({
      key,
      label: rowLabel,
      icon,
      said,
      mark,
    })),
    filters: g.filters,
    onToggleFilter: g.onToggleFilter,
    onResetFilters: g.onResetFilters,
  }));

  return (
    <div className="rail-view">
      <NavRail
        groups={railGroups}
        active={active}
        onSelect={select}
        label={label}
        searchable={searchable}
        foot={foot}
        collapsible={collapsible}
      />
      <div className="rail-view__pane">
        {chrome === "header" && (
          <ViewHeader
            title={section.title ?? section.label}
            meta={section.meta ?? meta}
            actions={section.actions ?? actions}
          />
        )}
        <div
          className={`rail-view__body${
            chrome === "bare" ? " rail-view__body--bare" : ""
          }`}
        >
          {/* Keyed on the open section so the boundary resets when the rail
              moves — a caught error belongs to the pane that threw it, and
              without the key the next section inherits its dead state. */}
          <ErrorBoundary key={active} what={section.title ?? section.label}>
            {Body ? (
              <Body />
            ) : section.render ? (
              <Rendered fn={section.render} />
            ) : null}
          </ErrorBoundary>
        </div>
      </div>
    </div>
  );
}
