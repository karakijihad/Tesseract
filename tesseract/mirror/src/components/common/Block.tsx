import type { ReactNode } from "react";

import { Hint } from "../ui/Hint";

export type BlockTone = "default" | "warn" | "bad";

interface BlockProps {
  /** The block's name. Always `t-ui` — a block heading is one rank, and the
   *  reason the app had 19px mono headings next to 11px ones is that each
   *  surface picked its own.
   *
   *  `null` for a block that IS its section: the rail's head already names it,
   *  and printing the name twice, six pixels apart, is what the operator saw
   *  on every view (2026-08-14). The meta line survives — a count belongs to
   *  the thing it counts. */
  title?: ReactNode | null;
  /** What the block MEANS, on an `ⓘ` beside the title. For a block whose name
   *  is accurate and still not self-explanatory — "Last persisted intent" is
   *  the case it was added for. Prose, not a repeat of the title; the marker
   *  only renders when there is something to say. */
  titleHint?: string;
  /** Right-aligned value on the title line — a count, a total, a state. */
  meta?: ReactNode;
  /** State of the thing the block describes, not decoration: `warn` while it
   *  needs attention, `bad` when it failed. */
  tone?: BlockTone;
  testId?: string;
  children: ReactNode;
}

/** A titled card inside a view — the app's one grouping unit.
 *
 * Every surface had invented this: a heading, some rows, a border, in its own
 * type and its own spacing. One block means a settings section, an autonomy
 * pane and a workspace group are the same object at the same rank, which is
 * what makes them read as one app rather than several.
 */
export function Block({
  title,
  titleHint,
  meta,
  tone = "default",
  testId,
  children,
}: BlockProps) {
  return (
    <section
      className={`block${tone === "default" ? "" : ` block--${tone}`}`}
      data-testid={testId}
    >
      {(title !== null || (meta !== undefined && meta !== null)) && (
        <div className={`block__head${title === null ? " block__head--meta-only" : ""}`}>
          {title !== null && (
            <span className="block__title t-ui">
              {title}
              {titleHint && (
                <Hint label={titleHint} maxWidth={420}>
                  <span className="block__title-hint" aria-label="what this means">
                    ⓘ
                  </span>
                </Hint>
              )}
            </span>
          )}
          {meta !== undefined && meta !== null && (
            <span className="block__meta t-meta">{meta}</span>
          )}
        </div>
      )}
      <div className="block__body">{children}</div>
    </section>
  );
}
