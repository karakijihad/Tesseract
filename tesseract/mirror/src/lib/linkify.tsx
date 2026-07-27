/**
 * linkifyText — turn URL-bearing plain text into highlighted-link JSX.
 *
 * Operator directive 2026-05-16: "links always should be highlighted
 * anywhere so that we know. even if a text such as [link](text)."
 *
 * Three forms are recognised and converted to `<a>` elements with the
 * `linked-anchor` class (styled in globals.css):
 *
 *   1. Markdown `[text](url)`            → <a href="url">text</a>
 *   2. Bare URLs `https://foo/bar`       → <a href="https://foo/bar">https://foo/bar</a>
 *      (also `http://` and `www.foo.bar`)
 *   3. Angle-bracket autolinks `<url>`   → <a href="url">url</a>
 *
 * Anchors always open in a new tab with `noopener noreferrer`. Trailing
 * sentence punctuation (`.,;:!?)`) is NOT swallowed into the URL.
 *
 * For markdown-heavy surfaces (chat bubbles, soul tab), use
 * `react-markdown`; this helper is for plain-text surfaces that
 * shouldn't introduce a full markdown pipeline (workspace event
 * bodies, channel messages, brief prose paragraphs, observer feed).
 */
import { Fragment, type ReactNode } from 'react';

// Matches three families in priority order:
//   group 1: markdown [text](url)            — captures [text]( and )
//   group 2: angle-bracket autolink <url>    — captures < and >
//   group 3: bare URL                        — http(s)://… or www.…
//
// The bare-URL alternate ALLOWS parens in the URL so wiki-style links
// like https://en.wikipedia.org/wiki/Foo_(disambiguation) match in
// full. The trailing-paren-balancer in stripTrailing() then strips
// any unmatched close-parens — so "(see https://foo.com)" still has
// the bare ")" returned as the trail without breaking the wiki case.
const LINK_RE = new RegExp(
  [
    String.raw`\[([^\]\n]+)\]\((https?:\/\/[^\s)]+|www\.[^\s)]+|\/[^\s)]+)\)`,
    String.raw`<(https?:\/\/[^\s>]+|www\.[^\s>]+)>`,
    String.raw`(https?:\/\/[^\s<>\[\]]+|www\.[^\s<>\[\]]+)`,
  ].join('|'),
  'gi',
);

// Sentence punctuation excluding `)` — paren-balancing handles ) separately.
const TRAILING_PUNCT = /[.,;:!?]+$/;

function ensureHref(raw: string): string {
  // Plain www.foo.com bare matches need a protocol for the href attr.
  if (raw.startsWith('http://') || raw.startsWith('https://') || raw.startsWith('/')) {
    return raw;
  }
  return `https://${raw}`;
}

function stripTrailing(url: string): { url: string; trail: string } {
  // First peel off any unmatched trailing `)` — handles
  // "(see https://foo.com)" by stripping the sentence-close paren
  // while leaving Wikipedia's balanced `Foo_(disambiguation)` intact.
  let stripped = url;
  let trail = '';
  while (stripped.endsWith(')')) {
    const opens = (stripped.match(/\(/g) ?? []).length;
    const closes = (stripped.match(/\)/g) ?? []).length;
    if (closes <= opens) break;
    trail = ')' + trail;
    stripped = stripped.slice(0, -1);
  }
  // Then peel off ordinary sentence-end punctuation.
  const m = stripped.match(TRAILING_PUNCT);
  if (m) {
    trail = m[0] + trail;
    stripped = stripped.slice(0, -m[0].length);
  }
  return { url: stripped, trail };
}

export function linkifyText(text: string): ReactNode {
  if (!text) return text;
  const out: ReactNode[] = [];
  let lastIndex = 0;
  let key = 0;

  // RegExp.exec stateful loop — LINK_RE has the `g` flag so each call
  // advances `lastIndex`. Reset before use because the module-scope
  // RegExp instance is shared across calls.
  LINK_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = LINK_RE.exec(text)) !== null) {
    const start = m.index;
    const end = start + m[0].length;
    if (start > lastIndex) {
      out.push(<Fragment key={`t${key++}`}>{text.slice(lastIndex, start)}</Fragment>);
    }
    const [, mdLabel, mdUrl, angleUrl, bareUrl] = m;
    if (mdUrl !== undefined) {
      out.push(
        <a
          key={`l${key++}`}
          href={ensureHref(mdUrl)}
          target="_blank"
          rel="noopener noreferrer"
          className="linked-anchor"
        >
          {mdLabel}
        </a>,
      );
      lastIndex = end;
      continue;
    }
    if (angleUrl !== undefined) {
      out.push(
        <a
          key={`l${key++}`}
          href={ensureHref(angleUrl)}
          target="_blank"
          rel="noopener noreferrer"
          className="linked-anchor"
        >
          {angleUrl}
        </a>,
      );
      lastIndex = end;
      continue;
    }
    if (bareUrl !== undefined) {
      const { url, trail } = stripTrailing(bareUrl);
      out.push(
        <a
          key={`l${key++}`}
          href={ensureHref(url)}
          target="_blank"
          rel="noopener noreferrer"
          className="linked-anchor"
        >
          {url}
        </a>,
      );
      if (trail) {
        out.push(<Fragment key={`p${key++}`}>{trail}</Fragment>);
      }
      lastIndex = end;
      continue;
    }
  }
  if (lastIndex < text.length) {
    out.push(<Fragment key={`t${key++}`}>{text.slice(lastIndex)}</Fragment>);
  }
  return out.length === 1 ? out[0] : <>{out}</>;
}

interface LinkifiedProps {
  text: string;
  className?: string;
  as?: 'span' | 'p' | 'div' | 'li';
}

/**
 * Wrapper component for the common case — pass `text` and a wrapping
 * element type. Use `<Linkified text={x} />` instead of
 * `<span>{linkifyText(x)}</span>` for clarity at the call site.
 */
export function Linkified({ text, className, as = 'span' }: LinkifiedProps) {
  const content = linkifyText(text);
  if (as === 'p') return <p className={className}>{content}</p>;
  if (as === 'div') return <div className={className}>{content}</div>;
  if (as === 'li') return <li className={className}>{content}</li>;
  return <span className={className}>{content}</span>;
}
