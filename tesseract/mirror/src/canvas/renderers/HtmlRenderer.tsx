// Y-2 — html surface. Renders arbitrary markup inside a sandboxed iframe
// via `srcDoc`. Sandbox WITHOUT `allow-same-origin` keeps the markup in an
// opaque origin — it cannot reach the Mirror DOM, cookies, or backend,
// which is the safe way to host tool-authored HTML on the canvas.
// Accepts `props.html` / `props.text` / `props.content` / `props.body` —
// tool authors pick inconsistent keys, and a wrong key used to render an
// empty (black) card.

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

export function HtmlRenderer({ descriptor }: RendererProps) {
  const props = descriptor.props ?? {};
  const html = String(props.html ?? props.text ?? props.content ?? props.body ?? '');
  return (
    <iframe
      className="surface-html"
      title={descriptor.title ?? 'html'}
      srcDoc={withStorageShim(html)}
      sandbox="allow-scripts"
      referrerPolicy="no-referrer"
    />
  );
}
