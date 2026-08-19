import type { ReactNode } from "react";

interface ViewHeaderProps {
  /** The name of the surface. Always `t-sub` — one rank above a `Block`
   *  title, which is what makes a view read as containing its blocks.
   *
   *  Omitted only when the surface is named by whatever frames it — the pulse
   *  stream rendered as a canvas card wears the card's title, and repeating it
   *  inside would name the same thing twice. */
  title?: ReactNode;
  /** The view's one-line state: counts, what is running, when it last ran. */
  meta?: ReactNode;
  /** Actions for the view as a whole — `Button`, right-aligned. */
  actions?: ReactNode;
}

/** The head of a view — the app's one.
 *
 * Seven views had written this by hand and no two agreed: mono uppercase at
 * caption rank in five of them, UI-font uppercase in Pulse, display rank in
 * Autonomy, each with its own refresh button. A view head is one act, so it is
 * one component: the type, the rule under it, the spacing and where the
 * actions sit belong here rather than to whoever writes the next view.
 */
export function ViewHeader({ title, meta, actions }: ViewHeaderProps) {
  return (
    <header className="view-head">
      {title !== undefined && <h1 className="view-head__title t-sub">{title}</h1>}
      {meta !== undefined && meta !== null && (
        <span className="view-head__meta t-meta">{meta}</span>
      )}
      {actions && <div className="view-head__actions">{actions}</div>}
    </header>
  );
}
