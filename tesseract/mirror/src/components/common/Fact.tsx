// One labelled fact in a card. A label above, a value under it.
//
// Every level-3 card on the Autonomy panel is the same act: a sentence, then a
// grid of these, then a list of state lines. It lived inside `EntryCard` until
// a second card needed it, which is the moment a convention has to become a
// component.

import type { ReactNode } from "react";

export function Fact({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="entry-fact">
      <span className="entry-fact__label t-meta t-label">{label}</span>
      <span className="entry-fact__value">{children}</span>
    </div>
  );
}
