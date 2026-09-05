// What the canvas is allowed to paint with, read out of the brand file.
//
// A canvas takes colour strings, not CSS. That is the one place in this app
// where a component could legitimately claim it has to write a hex, and it
// does not: every value here is read from `tokens.css` through the document,
// so changing a token still changes the picture and the Appearance panel still
// reaches it. Nothing below names a colour, a family or a size.
//
// The label FONT is read off the canvas element and not off the token, and
// that distinction is load-bearing. `--t-caption-size` is
// `calc(11px * var(--type-scale))`, and an unregistered custom property's
// computed value substitutes `var()` without evaluating `calc()`: reading the
// token gives the string `calc(11px * 1)`, which is not a font size, and
// `ctx.font = <invalid>` is ignored in silence. The stylesheet puts the
// caption tier on `.graph-canvas`, so the element's own computed `font-size`
// is a real number of pixels and the labels still follow the operator's
// text-size control.

/** Node colour by kind. One categorical colour each, and a kind added to the
 *  builder without one here draws in `fallback` rather than borrowing another
 *  kind's meaning. */
const KIND_TOKEN: Record<string, string> = {
  memory: '--graph-memory',
  wiki_page: '--graph-wiki',
  raw_source: '--graph-source',
  entity: '--graph-entity',
  run: '--graph-run',
  feedback: '--graph-feedback',
  tree: '--graph-tree',
  tag: '--graph-tag',
  agenda: '--graph-agenda',
  capability: '--graph-capability',
  playbook: '--graph-playbook',
};

const TOKENS = {
  ink: '--ink-100',
  label: '--ink-55',
  // A link's own token, not the surface wash ramp. See the note beside them in
  // `tokens.css`: a wash is a panel fill, and one used as a stroke drew every
  // link at alpha 7 of 255.
  link: '--graph-link',
  crossing: '--graph-link-crossing',
  fallback: '--neutral',
} as const;

export interface Brand {
  /** The colour for a kind, already resolved. */
  of: (kind: string) => string;
  ink: string;
  label: string;
  /** A link between two records in the same compartment. */
  link: string;
  /** A link between two compartments, which is the one worth spotting. */
  crossing: string;
  /** A ready `ctx.font` string at the app's caption tier. */
  caption: string;
  /** The same at half a step heavier, for whatever is being pointed at. */
  captionLoud: string;
}

/** Read every brand value the picture needs, once.
 *
 * Called on mount and whenever the canvas is resized rather than on every
 * frame: `getComputedStyle` forces a style recalculation, and a drag at sixty
 * frames a second would pay for one per frame to learn nothing new. The cost
 * of that is a theme change reaching the picture on the next resize; the
 * alternative was a picture that recalculates the whole document to draw a
 * dot.
 */
export function readBrand(canvas: Element): Brand {
  const style = getComputedStyle(document.documentElement);
  const value = (token: string) => style.getPropertyValue(token).trim();

  // The element's own resolved type, not the token's text. See the note at
  // the top of this file: the token still decides it, through the stylesheet.
  const own = getComputedStyle(canvas);
  const size = own.fontSize;
  const family = own.fontFamily;
  const resolved: Record<string, string> = {};
  for (const [kind, token] of Object.entries(KIND_TOKEN)) {
    resolved[kind] = value(token);
  }
  const fallback = value(TOKENS.fallback);

  return {
    of: (kind) => resolved[kind] || fallback,
    ink: value(TOKENS.ink),
    label: value(TOKENS.label),
    link: value(TOKENS.link),
    crossing: value(TOKENS.crossing),
    caption: `${size} ${family}`,
    captionLoud: `600 ${size} ${family}`,
  };
}

export { KIND_TOKEN };
