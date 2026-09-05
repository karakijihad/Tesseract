// The bridge that makes an authored page obey, injected rather than authored.
//
// The plan for this phase was a small protocol the assistant writes into every
// page it builds. That is the shape this folder exists to distrust: the
// founding measurement here is a correct sentence that reached the payload on
// zero turns, and a listener the model has to remember to write is the same
// bet. So the renderer splices the listener in, the way it already splices the
// storage shim in, and a page is controllable because it is an authored page,
// not because someone remembered.
//
// What the page owes is a name on each control, and a plain `<button>Deal
// </button>` already has one: `data-control`, then `id`, then `name`, then the
// button's own text. A page that wants a semantic event instead of a click
// calls `window.tesseract.emit(name, detail)`, and a page whose state is not
// in its form fields assigns `window.tesseract.state`. Both are one line and
// neither is required.
//
// Three message shapes and no versioning, because the whole point is that
// there is nothing here to get wrong:
//
//   in   { tesseract: 'command', id, action, target, value }
//   out  { tesseract: 'result',  id, ok, error, state }
//   out  { tesseract: 'event',   event, target, value }
//
// **The two directions are not the same decision.** Commands in are always
// available: reading or pressing a card the assistant drew is inert and
// useful on any of them. Events out are opt-in per card, because an event now
// wakes a turn, and a chart or a report with a link in it would spend a model
// call every time the operator clicked something. A card asks for the return
// leg with `props.live`, at creation or later through `surface_update`.

/** What `surface_control` can ask any authored page. */
export const HTML_CONTROLS = ['press', 'set', 'read'];

/** Elements a control name can be resolved against. Deliberately the things a
 *  person could point at and press, so `read` returns a vocabulary the model
 *  can use rather than a DOM dump. */
const NAMEABLE = 'button, input, select, textarea, a[href], [data-control]';

/** The listener spliced into every authored page.
 *
 *  `live` decides only the OUTBOUND half. A page that is not live still
 *  answers `press`, `set` and `read`; it just does not volunteer anything.
 */
export function pageBridge(live: boolean): string {
  return `<script>
(function () {
  function label(el) {
    if (!el || el.nodeType !== 1) return '';
    var attr = el.getAttribute('data-control') || el.id || el.getAttribute('name');
    if (attr) return String(attr);
    return (el.textContent || '').trim().slice(0, 40);
  }
  function all() { return document.querySelectorAll('${NAMEABLE}'); }
  function names() {
    var out = [], seen = {}, els = all();
    for (var i = 0; i < els.length; i++) {
      var n = label(els[i]);
      if (n && !seen[n]) { seen[n] = 1; out.push(n); }
    }
    return out;
  }
  function find(target) {
    var t = target == null ? '' : String(target);
    if (!t) return null;
    var els = all(), lower = t.toLowerCase(), i;
    for (i = 0; i < els.length; i++) if (label(els[i]) === t) return els[i];
    for (i = 0; i < els.length; i++) if (label(els[i]).toLowerCase() === lower) return els[i];
    return document.getElementById(t);
  }
  function readValue(el) {
    if (el.type === 'checkbox' || el.type === 'radio') return !!el.checked;
    return 'value' in el ? el.value : null;
  }
  function snapshot() {
    var values = {}, els = document.querySelectorAll('input, select, textarea');
    for (var i = 0; i < els.length; i++) {
      var n = label(els[i]);
      if (n) values[n] = readValue(els[i]);
    }
    var state = { controls: names(), values: values };
    var own = window.tesseract && window.tesseract.state;
    if (own !== undefined && own !== null) state.page = own;
    return state;
  }
  var LIVE = ${live ? "true" : "false"};
  function send(msg) {
    try { parent.postMessage(JSON.stringify(msg), '*'); } catch (err) { /* nothing to do */ }
  }
  function report(msg) {
    // The return leg. Silent unless this card was asked for it.
    if (LIVE) send(msg);
  }
  function truthy(v) {
    var s = String(v == null ? '' : v).toLowerCase();
    return s !== '' && s !== 'false' && s !== '0' && s !== 'off' && s !== 'no';
  }
  function act(cmd) {
    if (cmd.action === 'read') return '';
    var el = find(cmd.target);
    if (!el) {
      var have = names();
      return 'no control named "' + cmd.target + '" on this page. It has: ' +
        (have.length ? have.join(', ') : 'none');
    }
    if (cmd.action === 'press') { el.click(); return ''; }
    if (cmd.action === 'set') {
      if (el.type === 'checkbox' || el.type === 'radio') el.checked = truthy(cmd.value);
      else el.value = cmd.value == null ? '' : String(cmd.value);
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      return '';
    }
    return 'this page cannot ' + cmd.action;
  }
  window.addEventListener('message', function (ev) {
    // Only the card takes this page's orders. Origin is useless here (an
    // opaque origin reads as null), so identity is the same question the
    // parent asks of us: did the window on the other side of this card send
    // it. Without the check a third-party frame nested INSIDE an authored
    // page could press its parent's controls.
    if (ev.source !== parent) return;
    var d = ev.data;
    if (typeof d === 'string') { try { d = JSON.parse(d); } catch (err) { return; } }
    if (!d || d.tesseract !== 'command' || !d.id) return;
    var error = '';
    try { error = act(d); } catch (err) { error = (err && err.message) || String(err); }
    send({ tesseract: 'result', id: d.id, ok: !error, error: error, state: snapshot() });
  });
  document.addEventListener('click', function (ev) {
    for (var el = ev.target; el && el.nodeType === 1; el = el.parentElement) {
      if (el.matches && el.matches('button, a[href], [data-control]')) {
        var n = label(el);
        if (n) report({ tesseract: 'event', event: 'clicked', target: n, value: null });
        return;
      }
    }
  }, true);
  document.addEventListener('change', function (ev) {
    var n = label(ev.target);
    if (n) report({ tesseract: 'event', event: 'edited', target: n, value: readValue(ev.target) });
  }, true);
  window.tesseract = {
    state: null,
    live: LIVE,
    emit: function (name, detail) {
      report({
        tesseract: 'event',
        event: 'clicked',
        target: String(name),
        value: detail === undefined ? null : detail
      });
    }
  };
})();
</script>`;
}
