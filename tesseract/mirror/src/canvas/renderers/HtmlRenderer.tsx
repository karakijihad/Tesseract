// Y-2 — html surface. Renders arbitrary markup inside a sandboxed iframe
// via `srcDoc`. Sandbox WITHOUT `allow-same-origin` keeps the markup in an
// opaque origin — it cannot reach the Mirror DOM, cookies, or backend,
// which is the safe way to host tool-authored HTML on the canvas.
// Accepts `props.html` / `props.text` / `props.content` / `props.body` —
// tool authors pick inconsistent keys, and a wrong key used to render an
// empty (black) card.

import { useEffect } from 'react';

import { Note } from '../../components/common/Note';
import type { RendererProps } from './index';

// An opaque origin throws `SecurityError` on `window.localStorage` /
// `window.sessionStorage` access, which kills tool-authored scripts that
// probe for storage. Shim both with in-memory Storage-shaped objects before
// any surface markup runs.
const STORAGE_SHIM = `<script>
(function () {
  function memoryStorage() {
    var data = Object.create(null);
    var keys = function () { return Object.keys(data); };
    return {
      getItem: function (k) {
        k = String(k);
        return Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null;
      },
      setItem: function (k, v) { data[String(k)] = String(v); },
      removeItem: function (k) { delete data[String(k)]; },
      clear: function () { data = Object.create(null); },
      key: function (i) {
        var all = keys();
        i = Number(i);
        return i >= 0 && i < all.length ? all[i] : null;
      },
      get length() { return keys().length; }
    };
  }
  function install(name) {
    try {
      var probe = window[name];
      if (probe) { probe.getItem(''); return; }
    } catch (err) { /* SecurityError — fall through and shim */ }
    var store = memoryStorage();
    try {
      Object.defineProperty(window, name, {
        value: store,
        configurable: true,
        writable: true
      });
    } catch (err) {
      try { window[name] = store; } catch (err2) { /* nothing else to try */ }
    }
  }
  install('localStorage');
  install('sessionStorage');
})();
</script>`;

// A `<script>` placed before a document's `<!doctype html>` makes the doctype
// no longer the first token, so the parser discards it and renders in quirks
// mode — silently altering layout for EVERY html surface, not just ones that
// use storage. Splice the shim in immediately after a leading doctype when one
// is present; otherwise prepend it (a doctype-less fragment already parses in
// quirks mode, so prepending changes nothing).
const DOCTYPE_RE = /^\s*<!doctype[^>]*>/i;

function withStorageShim(html: string): string {
  const match = html.match(DOCTYPE_RE);
  if (match) {
    const end = match[0].length;
    return html.slice(0, end) + STORAGE_SHIM + html.slice(end);
  }
  return STORAGE_SHIM + html;
}

// A frame nested inside this one inherits the opaque origin, so an embedded
// player (YouTube et al.) throws reading storage at boot and paints black. The
// markup is left exactly as authored — a nested frame that needs no storage
// works fine, and removing it would break the ones that do — but the surface
// says what is about to happen and which verb plays media instead, rather than
// handing the operator a black rectangle and no reason.
//
// Matched rather than parsed: this is a caption on the card, not a security
// boundary. The sandbox is the boundary and it holds whatever this reads.
const NESTED_FRAME_RE = /<iframe\b[^>]*\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))/gi;

function nestedFrameSources(html: string): string[] {
  const found: string[] = [];
  for (const match of html.matchAll(NESTED_FRAME_RE)) {
    const src = match[1] ?? match[2] ?? match[3] ?? '';
    if (/^https?:\/\//i.test(src)) {
      found.push(src);
    }
  }
  return found;
}

export function HtmlRenderer({ descriptor, report }: RendererProps) {
  const props = descriptor.props ?? {};
  const html = String(props.html ?? props.text ?? props.content ?? props.body ?? '');
  const nested = nestedFrameSources(html);

  // The caption below tells the operator; this tells the model. Without it
  // the card is registered, drawn, and black, and `surface_list` calls that
  // a surface like any other.
  useEffect(() => {
    if (!report) return;
    if (html === '') {
      report('errored', 'no markup: props carried none of html / text / content / body');
    } else if (nested.length > 0) {
      report(
        'degraded',
        `embeds ${nested.length} third-party page(s) — sandboxed into an opaque origin, so an embedded player paints black here. Use open target:"${nested[0]}" instead.`,
      );
    }
  }, [report, html, nested.length, nested[0]]);

  const frame = (
    <iframe
      className="surface-html"
      title={descriptor.title ?? 'html'}
      srcDoc={withStorageShim(html)}
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
    />
  );
  if (nested.length === 0) {
    return frame;
  }
  return (
    <div className="surface-html-nested">
      <Note tone="warn">
        This surface embeds {nested.length === 1 ? 'a page' : `${nested.length} pages`} from another
        site. Authored markup is sandboxed into an opaque origin, so an embedded player renders as a
        black pane here. To show something that already exists, use{' '}
        <code>open target:"{nested[0]}"</code> — the runtime picks the surface and grants a player
        what it needs.
      </Note>
      {frame}
    </div>
  );
}
