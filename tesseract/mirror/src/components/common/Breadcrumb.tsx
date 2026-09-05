import type { ReactNode } from "react";

export interface Crumb {
  /** Stable id for the level this crumb returns to. */
  key: string;
  label: string;
}

interface BreadcrumbProps {
  /** Root first, current last. The last crumb is where you are and does not
   *  navigate. */
  crumbs: Crumb[];
  /** Called with the index the operator wants to return to. */
  onGo: (index: number) => void;
  /** Right-aligned, for the hint about how to go back. */
  trailing?: ReactNode;
}

/** Where you are, and the way back up.
 *
 * The app opened detail in a modal over the surface that raised it, which is
 * the one thing an operational panel cannot do: a floor plan you cannot see
 * past has stopped being a floor plan. A level opens inside the pane instead,
 * and this is how you come back — so it is navigation, not decoration, and it
 * is a component rather than a shape each room draws for itself.
 */
export function Breadcrumb({ crumbs, onGo, trailing }: BreadcrumbProps) {
  return (
    <nav className="crumbs" aria-label="Where you are">
      {crumbs.map((crumb, i) => {
        const here = i === crumbs.length - 1;
        return (
          <span className="crumbs__step" key={crumb.key}>
            {i > 0 && (
              <span className="crumbs__sep" aria-hidden="true">
                ›
              </span>
            )}
            {here ? (
              <span className="crumb crumb--here" aria-current="page">
                {crumb.label}
              </span>
            ) : (
              <button type="button" className="crumb" onClick={() => onGo(i)}>
                {crumb.label}
              </button>
            )}
          </span>
        );
      })}
      {trailing && <span className="crumbs__trailing t-meta">{trailing}</span>}
    </nav>
  );
}
