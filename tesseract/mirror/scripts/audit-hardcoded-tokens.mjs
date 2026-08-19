/**
 * audit-hardcoded-tokens.mjs
 *
 * Scans src/**\/*.{ts,tsx,css} and public/**\/*.html for hard-coded values
 * that have canonical token replacements in tokens.css.
 *
 * Flags:
 *   - HEX colors:   #[0-9a-fA-F]{3,8}  (always has a token replacement)
 *   - Raw ms:       \d+ms  (motion tokens exist: --motion-fast/med/slow)
 *   - Colour literals: rgba()/hsl() whose first channel is a NUMBER. The same
 *     function reading a token is the correct form and passes.
 *   - Seconds: a duration written as `0.2s` / `26s` on a transition or
 *     animation — off the motion scale and unreachable from one file.
 *   - Primitive restyle: a selector naming a shared control (.btn, .input,
 *     .select, .textarea, .checkbox, .radio, .range, .color-well, .modal) from
 *     outside that control's own stylesheet, setting an APPEARANCE property.
 *     Placement is left alone.
 *   - Private button: a `<button>` wearing none of the declared control
 *     classes. The list IS the app's set of controls.
 *   - Clickable non-button: a `<div>`/`<li>`/`<span>` carrying an `onClick`
 *     that is not a `Row` or a `RowActions`. `<a>` is excluded — a link
 *     navigates, and the browser already gives it focus and Enter.
 *   - Raw field: an `<input>`/`<select>`/`<textarea>` wearing none of the
 *     declared field classes.
 *
 * A line (or its declaration) carrying `brand-exempt: <reason>` is allowed its
 * literal. The reason lives beside the value, so a survivor is explained
 * rather than merely tolerated.
 *
 *   - Raw px in FIVE property groups only: padding*, margin*, gap/row-gap/
 *     column-gap, border/outline WIDTH (the shorthand's width slot and the
 *     -width longhands), and letter-spacing. The space scale is numeric — the
 *     number IS the px value — so every one of these has a token.
 *
 * NOT flagged, and the exclusion is a DECISION rather than an oversight:
 *   - `width`, `height`, `min-*`, `max-*`, `top/right/bottom/left`, `inset*`,
 *     `grid-template-*`, `box-shadow` offsets and blur, `border-radius`,
 *     `outline-offset`, `999px` pill radii. These are per-component
 *     MEASUREMENTS, not brand values: a 176px rail width or a 480px max-height
 *     tokenised into tokens.css makes the brand file a junk drawer and this
 *     audit unsatisfiable. A surface decides how big it is; the brand file
 *     decides the rhythm between things.
 *   - Raw px anywhere in `.ts` / `.tsx`. Inline styles are review's job.
 *
 * HTML files (public/**\/*.html) are standalone documents with no access to
 * tokens.css, so only their <style> blocks are scanned — inline <script>
 * bodies and HTML attributes (e.g. <input type="color" value="#...">) are
 * implementation detail, not design surface, and are excluded to avoid
 * false positives.
 *
 * Within those <style> blocks, a hex on a CUSTOM PROPERTY DECLARATION
 * (`--bg-void: #050508`) is allowed and a hex used directly in a rule
 * (`background: #b02a2a`) is not — including when both sit on one line, since
 * only the declarations are stripped and the remainder is still scanned. A standalone document cannot import
 * tokens.css, so it has to define its own palette — flagging those
 * definitions would make the rule impossible to satisfy rather than
 * enforceable, which is what it did: the splash defines its tokens in a
 * `:root` block that says so in a comment, and the audit failed on every
 * line of it. The check that still bites is the one that matters, namely a
 * rule reaching past the document's own tokens for a literal.
 *
 * Exits 1 if any hex/ms violation is found outside src/styles/tokens.css.
 *
 * Run: node scripts/audit-hardcoded-tokens.mjs
 * Add to CI: "audit:tokens": "node scripts/audit-hardcoded-tokens.mjs"
 */

import { readFileSync, readdirSync, statSync } from 'fs';
import { join, relative } from 'path';
import { fileURLToPath } from 'url';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const SRC_DIR = join(__dirname, '..', 'src');
const PUBLIC_DIR = join(__dirname, '..', 'public');
const TOKENS_FILE = join(SRC_DIR, 'styles', 'tokens.css');

// Patterns
const HEX_RE = /#[0-9a-fA-F]{3,8}\b/g;
const MS_RE  = /(?<![a-zA-Z-])\b(\d+(?:\.\d+)?)ms\b/g;
// TYPE check — an absolute font-size outside tokens.css. The 7-tier scale is
// the app's hierarchy AND the operator's text-size control (every tier is a
// multiple of --type-scale), so a literal size is both an off-scale step and a
// surface that silently opts out of that setting. `em` and `%` are relative
// and deliberately allowed: markdown sizes its headings against its container.
const FONT_SIZE_RE = /font-size:\s*([0-9.]+(?:px|rem|pt))/g;
// Every `--token-name: <value>` declaration on a line, anchored to the start
// of the line or to a preceding `{` or `;` so it cannot match mid-value. The
// separator is captured and put back, leaving the rest of the line intact for
// scanning — the declaration is exempt, the line is not.
//
// `{` belongs in that set: `:root { --danger: #b02a2a; }` on one line is a
// declaration like any other, and anchoring to `^|;` alone reported it as a
// violation there while accepting the identical declaration on its own line.
const CUSTOM_PROPERTY_DECL_RE = /(^|[;{])\s*--[A-Za-z0-9-]+\s*:[^;}]*/g;
// COLOR check — a colour function whose first channel is a NUMBER. The same
// function reading a token (`hsl(var(--accent-hsl) / 0.15)`,
// `color-mix(in srgb, var(--ok) 12%, transparent)`) is the correct form and is
// not matched, which is what makes this rule satisfiable.
// The negated class already crosses newlines, so this matches a call written
// over several lines — but only when it is applied to the whole file, which is
// why the CSS pass below scans `content` rather than each line.
const COLOR_LITERAL_RE = /\b(?:rgba?|hsla?)\(\s*[0-9.][^)]*\)/g;
// SECONDS check — a duration written in seconds inside a transition or
// animation declaration. `26s` and `0.12s` both pass every other rule here
// because neither is a hex and neither is `ms`. The declaration is matched
// whole, across newlines, because that is how this codebase writes a
// multi-property transition.
const SECONDS_RE = /(?<![\w.-])[0-9]*\.?[0-9]+s(?![\w-])/g;
// Terminated by `;` OR by the rule's closing `}` — CSS lets the last
// declaration in a block drop its semicolon, and requiring one would reopen
// the same hole the multiline form just came through.
const MOTION_DECL_RE = /\b(?:transition|animation)[a-z-]*\s*:[^;{}]*[;}]/g;
// SPACE check — a raw px in one of the five property groups the numeric space
// scale covers. Written as an exact-match property list rather than a prefix
// so `border-radius`, `outline-offset` and `inset` cannot be swept in by
// accident: those are per-component measurements and are excluded on purpose
// (see the file header). `letter-spacing` wants `em`, not a space token — px
// tracking does not scale with the operator's text-size control.
const SPACE_PROP_RE =
  /^(?:padding|margin)(?:-(?:top|right|bottom|left))?$|^(?:row-|column-)?gap$/;
const STROKE_PROP_RE =
  /^(?:border|outline)(?:-(?:top|right|bottom|left))?(?:-width)?$|^outline-width$/;
const TRACKING_PROP_RE = /^letter-spacing$/;
// The separator and the whitespace after it are captured so the property's own
// offset can be computed. Reporting the MATCH's line instead pointed at the
// rule's `{` for the first declaration in a block — which put `brand-exempt`
// on the line above the declaration out of the exemption's own scan window,
// and named the wrong line in the failure output.
const DECLARATION_RE = /(^|[;{])(\s*)([a-z-]+)\s*:\s*([^;}]*)/g;
const RAW_PX_RE = /(?<![\w.-])(-?[0-9]*\.?[0-9]+)px(?![\w-])/g;

// PRIMITIVE check — the shared controls, and the ONE file each is allowed to
// style. A caller that reaches across a file boundary to repaint `.btn` is how
// six private button styles grew back after being deleted, and it is invisible
// to every other rule here because it uses tokens correctly while doing it.
const PRIMITIVE_HOMES = {
  '.btn': 'ui.css', '.select': 'ui.css',
  '.icon-btn': 'ui.css', '.close-btn-inline': 'ui.css',
  '.input': 'globals.css', '.textarea': 'globals.css',
  '.checkbox': 'globals.css', '.checkbox__box': 'globals.css',
  '.radio': 'globals.css', '.range': 'globals.css',
  '.color-well': 'globals.css',
  '.modal': 'ui.css',
};
// `.row` is deliberately NOT in that list. A row's SHAPE is the surface's —
// `.autonomy-row` is a bordered card, `.wt-tab` is a tab, `.activity-map__row`
// is a bare line, and their answers to being hovered cannot be one answer. The
// component owns the half that can be: the cursor, the focus ring, and the
// activation contract. Which shapes exist is closed by `ROW_CLASSES` below
// instead — a fourth one has to be named there.
// Appearance, not placement. Where a control SITS belongs to the surface
// around it (`.cost-row .checkbox__box { margin-right }` is correct); what it
// LOOKS LIKE belongs to the component.
const APPEARANCE_PROPERTY_RE =
  /(^|[;{])\s*(background|background-[a-z]+|color|border|border-[a-z-]+|box-shadow|padding|padding-[a-z]+|font|font-[a-z]+|outline|outline-[a-z]+|text-transform|letter-spacing)\s*:/;

/**
 * Walk dir and collect all files whose name matches extRe, excluding any
 * file in excludeFiles.
 */
function collectFiles(dir, extRe, excludeFiles = []) {
  const results = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) {
      results.push(...collectFiles(full, extRe, excludeFiles));
    } else if (extRe.test(entry)) {
      if (!excludeFiles.includes(full)) {
        results.push(full);
      }
    }
  }
  return results;
}

/**
 * Blank out every `/* … *​/` comment body, keeping the file's length and every
 * newline so line numbers still hold.
 *
 * The whole-file passes below (colour, seconds, own-token) need this because
 * they do not walk lines and so cannot use `isCommentLine`. Without it a
 * commented-out value — `/* was: rgba(255,180,84,0.12) *​/`, the note someone
 * leaves when they replace a literal with a token — fails the audit that
 * change was made to satisfy.
 */
function blankComments(text) {
  return text.replace(/\/\*[\s\S]*?\*\//g, (c) =>
    c.replace(/[^\n]/g, ' '),
  );
}

/**
 * Test whether a line is entirely a comment (trim leading whitespace first).
 */
function isCommentLine(line) {
  const t = line.trimStart();
  return (
    t.startsWith('//') ||
    t.startsWith('*') ||
    t.startsWith('/*') ||
    t.startsWith('<!--')
  );
}

/**
 * Strip string literals from TS/TSX lines so className="..." strings
 * don't produce false positives.
 */
function stripStringLiterals(line) {
  return line
    .replace(/"[^"]*"/g, '""')
    .replace(/'[^']*'/g, "''")
    .replace(/`[^`]*`/g, '``');
}

function scanFile(filePath) {
  const isHtml = filePath.endsWith('.html');
  const content = readFileSync(filePath, 'utf-8');
  const lines = content.split('\n');
  // What the whole-file passes read: same length, same line breaks, comment
  // bodies blanked, so a commented-out value cannot fail the audit.
  const code = blankComments(content);
  const violations = [];
  let inStyleBlock = false;

  lines.forEach((rawLine, idx) => {
    const lineNum = idx + 1;

    // HTML: only scan inside <style>...</style> — script bodies and tag
    // attributes are not design-token surface (see file header comment).
    if (isHtml) {
      if (/<style[^>]*>/.test(rawLine)) inStyleBlock = true;
      const wasInStyle = inStyleBlock;
      if (/<\/style>/.test(rawLine)) inStyleBlock = false;
      if (!wasInStyle) return;
    }

    if (isCommentLine(rawLine)) return;

    let line = filePath.endsWith('.css') || isHtml ? rawLine : stripStringLiterals(rawLine);

    // A standalone document defines the palette it cannot import, so a literal
    // in a custom-property DECLARATION is allowed. Strip the declarations and
    // scan what is left, rather than skipping the line: returning early here
    // exempted `--danger: #b02a2a; background: #cc0000;` in full, hiding the
    // second literal, which is a real violation sitting beside an allowed one.
    if (isHtml) line = line.replace(CUSTOM_PROPERTY_DECL_RE, '$1');

    // HEX check — every raw hex has a token replacement
    for (const m of line.matchAll(HEX_RE)) {
      violations.push({
        file: filePath,
        line: lineNum,
        type: 'hex',
        value: m[0],
        text: rawLine.trim(),
      });
    }

    // TYPE check — raw font-size, css only (inline styles in tsx are caught
    // by review, and stripStringLiterals would mangle the match anyway).
    if (filePath.endsWith('.css')) {
      for (const m of line.matchAll(FONT_SIZE_RE)) {
        violations.push({
          file: filePath,
          line: lineNum,
          type: 'font-size',
          value: m[1],
          text: rawLine.trim(),
        });
      }
    }

    // MS check — motion tokens: --motion-fast (120ms), --motion-med (250ms), --motion-slow (400ms)
    for (const m of line.matchAll(MS_RE)) {
      violations.push({
        file: filePath,
        line: lineNum,
        type: 'ms',
        value: m[0],
        text: rawLine.trim(),
      });
    }

    // COLOR check for TS/TSX — scanned on the RAW line, before
    // `stripStringLiterals`. That helper exists so a hex inside a `className`
    // is not a false positive, but it also blanked the one place a colour
    // function can legitimately appear in a component:
    // `style={{ color: 'rgba(255,0,0,.5)' }}`. A quoted string containing
    // `rgba(` with numeric channels is that, essentially always.
    //
    // HTML is handled after this loop, whole-file, alongside CSS.
    // Test files are skipped: a test asserting on a colour string is not a
    // surface that renders one, and every such assertion would otherwise have
    // to be exempted individually. `stripStringLiterals` used to hide them by
    // accident, which is why this only became visible when the check started
    // reading the raw line.
    if (
      !filePath.endsWith('.css') &&
      !isHtml &&
      !/\.test\.tsx?$/.test(filePath) &&
      !isExempt(lines, idx)
    ) {
      for (const m of rawLine.matchAll(COLOR_LITERAL_RE)) {
        violations.push({
          file: filePath, line: lineNum, type: 'color-literal',
          value: m[0], text: rawLine.trim(),
        });
      }
    }
  });

  // COLOR check, HTML — the `<style>` blocks only, with their custom-property
  // declarations stripped (a standalone document defines the palette it cannot
  // import). Scanned whole for the same reason CSS is: a colour function can
  // be written across lines, and splash.html is the file where a literal is
  // hardest to correct later — it compiles into the .exe and reaches no
  // install by update.
  if (isHtml) {
    for (const block of code.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)) {
      const body = block[1].replace(CUSTOM_PROPERTY_DECL_RE, '$1');
      const offset = block.index + block[0].indexOf(block[1]);
      for (const m of body.matchAll(COLOR_LITERAL_RE)) {
        const lineNum = code.slice(0, offset + m.index).split('\n').length;
        if (isExempt(lines, lineNum - 1)) continue;
        violations.push({
          file: filePath, line: lineNum, type: 'color-literal',
          value: m[0].replace(/\s+/g, ' '), text: lines[lineNum - 1].trim(),
        });
      }
    }
  }

  // COLOR check, CSS — an rgba()/hsl() built from digits is a copy of a token,
  // not a reference to one, so the operator's Appearance controls cannot reach
  // it. tokens.css is where the numbers live; everywhere else names them.
  //
  // Scanned over the whole file rather than line by line: a colour function's
  // arguments can be split across lines, which is exactly how the seconds
  // check was defeated before it was rewritten the same way.
  if (filePath.endsWith('.css')) {
    for (const m of code.matchAll(COLOR_LITERAL_RE)) {
      const lineNum = code.slice(0, m.index).split('\n').length;
      if (isExempt(lines, lineNum - 1)) continue;
      violations.push({
        file: filePath, line: lineNum, type: 'color-literal',
        value: m[0].replace(/\s+/g, ' '),
        text: lines[lineNum - 1].trim(),
      });
    }
  }

  // SECONDS check — the same argument as the colour one, for time: a raw `0.2s`
  // in a transition is off the motion scale and outside anything that could
  // retime the app in one place.
  //
  // Scanned per DECLARATION rather than per line. A per-line test for the words
  // `transition`/`animation` missed the shape this codebase actually writes —
  //     transition:
  //       transform 0.22s ease,
  //       opacity 0.22s ease;
  // — where the keyword line has no digits and the value lines have no keyword.
  // Two live literals sat behind that hole and the audit reported PASS.
  if (filePath.endsWith('.css')) {
    for (const decl of code.matchAll(MOTION_DECL_RE)) {
      const lineNum = code.slice(0, decl.index).split('\n').length;
      if (isExempt(lines, lineNum - 1)) continue;
      for (const m of decl[0].matchAll(SECONDS_RE)) {
        violations.push({
          file: filePath, line: lineNum, type: 'seconds',
          value: m[0], text: decl[0].replace(/\s+/g, ' ').trim().slice(0, 90),
        });
      }
    }
  }

  // SPACE / STROKE / TRACKING check — a raw px in one of the five property
  // groups the numeric scale covers. Per DECLARATION, over the whole file, for
  // the same reason the seconds check is: `padding:\n  8px 12px;` is how this
  // codebase writes a long shorthand, and a per-line test sees no property
  // name on the value line.
  //
  // CSS only. `public/**/*.html` is a standalone document that cannot import
  // tokens.css, so demanding a token there would be unsatisfiable rather than
  // enforceable — the same argument that exempts its palette declarations.
  if (filePath.endsWith('.css')) {
    for (const m of code.matchAll(DECLARATION_RE)) {
      const prop = m[3];
      const type = SPACE_PROP_RE.test(prop)
        ? 'space-px'
        : STROKE_PROP_RE.test(prop)
          ? 'stroke-px'
          : TRACKING_PROP_RE.test(prop)
            ? 'tracking-px'
            : null;
      if (!type) continue;
      const propIndex = m.index + m[1].length + m[2].length;
      const lineNum = code.slice(0, propIndex).split('\n').length;
      if (isExempt(lines, lineNum - 1)) continue;
      for (const p of m[4].matchAll(RAW_PX_RE)) {
        violations.push({
          file: filePath, line: lineNum, type,
          value: p[0], text: `${prop}: ${m[4].replace(/\s+/g, ' ').trim()}`,
        });
      }
    }
  }

  return violations;
}

/**
 * A line carrying `brand-exempt:` — on itself or on the line above — is
 * allowed its literal. The escape hatch is deliberate and deliberately loud:
 * the reason sits in the file next to the value, so the next reader sees why
 * rather than finding an unexplained survivor. The hue track in Appearance is
 * the case it exists for — a control that renders the hue axis has to name
 * hues.
 */
function isExempt(lines, idx) {
  // Scoped to the DECLARATION, so one comment covers a value spread over
  // several lines (a gradient's colour stops) without also excusing the next
  // property in the same rule. The cost is that a comment written above the
  // SELECTOR exempts nothing, which is the placement an author reaches for
  // first — so the failure output says where it goes rather than leaving them
  // to work it out. Widening to the rule would trade a surprise for a hole.
  for (let i = idx; i >= 0; i--) {
    if (/brand-exempt:/.test(lines[i])) return true;
    if (i < idx && /[;{]\s*$/.test(lines[i])) return false;
  }
  return false;
}

const files = [
  ...collectFiles(SRC_DIR, /\.(ts|tsx|css)$/, [TOKENS_FILE]),
  ...collectFiles(PUBLIC_DIR, /\.html$/, []),
];
const allViolations = [];

for (const f of files) {
  const v = scanFile(f);
  allViolations.push(...v);
}

/**
 * UNDEFINED-TOKEN check. A `var(--name)` naming a property tokens.css never
 * declares is not a soft failure — the declaration is invalid and dropped, so
 * the element renders with no background or no colour at all. 23 such names
 * accumulated across the stylesheets before anyone noticed, 44 of their
 * references carrying no fallback, because this audit only ever looked for
 * hexes and milliseconds — a token that does not exist has neither.
 *
 * Properties written from JS at render time are the one legitimate case, so
 * they are named here rather than inferred.
 */
const RUNTIME_SET_PROPERTIES = new Set(['--mic-level', '--wake-level', '--h']);
const VAR_REF_RE = /var\(\s*(--[A-Za-z0-9-]+)/g;
const DECL_RE = /(?:^|[;{])\s*(--[A-Za-z0-9-]+)\s*:/gm;

const declared = new Set(
  [...readFileSync(TOKENS_FILE, 'utf8').matchAll(DECL_RE)].map((m) => m[1]),
);

for (const f of files) {
  if (f === TOKENS_FILE) continue;
  // A test asserting on a string that CONTAINS `var(--x)` is not a surface
  // that renders one — `splash.test.ts` reads `public/splash.html` off disk
  // and asserts on the palette that document declares for itself, which this
  // check cannot see because it is seeded from tokens.css. Same exclusion, and
  // the same reason, as the colour-literal pass above.
  if (/\.test\.tsx?$/.test(f)) continue;
  const text = readFileSync(f, 'utf8');
  // Per FILE, seeded from tokens.css. A single shared set let a name declared
  // locally in one stylesheet vouch for every file walked after it, which is
  // the one thing this check exists to catch — and the leak was invisible
  // because the alphabetical walk usually put the declaration first.
  const visible = new Set(declared);
  text.split(/\r?\n/).forEach((line, i) => {
    // A local declaration on this line defines the name for this file.
    for (const m of line.matchAll(DECL_RE)) visible.add(m[1]);
    for (const m of line.matchAll(VAR_REF_RE)) {
      const name = m[1];
      if (visible.has(name) || RUNTIME_SET_PROPERTIES.has(name)) continue;
      allViolations.push({
        file: f,
        line: i + 1,
        type: 'undefined-token',
        value: name,
        text: line.trim(),
      });
    }
  });
}

/**
 * PRIMITIVE-RESTYLE check. Walks every CSS rule and reports one whose selector
 * names a shared control from outside that control's own stylesheet, when the
 * body sets an appearance property.
 *
 * This is the check the divergence needs, and the one the others cannot make:
 * `.cost-row .btn { background: var(--bg-card) }` uses a token perfectly and
 * still produces a second button. Placement is left alone on purpose — a
 * surface decides where a control sits, the component decides what it is.
 *
 * KNOWN LIMIT — this is a regex, not a CSS parser. It cannot represent
 * nesting, so a rule body that MIXES direct declarations with a nested block
 * (`.cost-row .btn { background: …; &:hover { … } }`) is skipped entirely
 * rather than reported. Rules inside `@media`/`@supports` are covered, because
 * the wrapper fails to match and the scan finds the inner rule on its own —
 * verified with a fixture. No CSS nesting is in use in this tree; if it is
 * adopted, this pass needs a real parser rather than a wider regex.
 */
const RULE_RE = /([^{}]+)\{([^{}]*)\}/g;

/**
 * OWN-TOKEN check. A custom property declared outside tokens.css whose value
 * is a LITERAL — a component inventing its own brand value, which is what the
 * brand rule ("no other file declares a custom property") is aiming at.
 *
 * An element-scoped alias that FORWARDS a token (`--swatch-h: var(--accent-h)`
 * on the swatch it colours) is the legitimate use and is allowed: it carries
 * no value of its own, so the brand file still decides what it is.
 */
const LOCAL_DECL_RE = /(?:^|[;{])\s*(--[A-Za-z0-9-]+)\s*:\s*([^;}]+)/g;

for (const f of files) {
  if (!f.endsWith('.css') || f === TOKENS_FILE) continue;
  const text = readFileSync(f, 'utf8');
  const lines = text.split(/\r?\n/);
  const code = blankComments(text);
  for (const m of code.matchAll(LOCAL_DECL_RE)) {
    const value = m[2].trim();
    // A local alias that reaches a token carries no value of its own, so the
    // brand file still decides what it is. `calc(var(--space-sm) * 2)` counts.
    if (value.includes('var(')) continue;
    const lineNum = code.slice(0, m.index).split('\n').length;
    if (isExempt(lines, lineNum - 1)) continue;
    allViolations.push({
      file: f, line: lineNum, type: 'own-token',
      value: `${m[1]}: ${value.slice(0, 40)}`,
      text: lines[lineNum - 1].trim(),
    });
  }
}

for (const f of files) {
  if (!f.endsWith('.css') || f === TOKENS_FILE) continue;
  const text = readFileSync(f, 'utf8');
  const base = f.split(/[\\/]/).pop();
  for (const rule of blankComments(text).matchAll(RULE_RE)) {
    const selector = rule[1];
    const body = rule[2];
    for (const [cls, home] of Object.entries(PRIMITIVE_HOMES)) {
      if (base === home) continue;
      // Word-boundaried so `.input` does not match `.input-wide`.
      if (!new RegExp(`\\${cls}(?![\\w-])`).test(selector)) continue;
      if (!APPEARANCE_PROPERTY_RE.test(body)) continue;
      if (/brand-exempt:/.test(selector)) continue;
      allViolations.push({
        file: f,
        line: text.slice(0, rule.index).split('\n').length,
        type: 'primitive-restyle',
        value: `${cls} (owned by ${home})`,
        text: selector.trim().replace(/\s+/g, ' ').slice(0, 90),
      });
    }
  }
}

/**
 * UNSTYLED-BUTTON check. A `<button className="x">` where no stylesheet gives
 * `.x` a base rule renders as a raw browser button — grey chrome on a dark
 * cockpit, next to siblings that look nothing like it.
 *
 * `.autonomy-btn` had NO rule anywhere and `.session-action` had only
 * `.session-action.is-confirming`; between them thirteen Approve / Reject /
 * Cancel / Delete buttons across seven files rendered unstyled, and it stayed
 * that way because every other check here reads stylesheets and asks whether
 * the VALUES are tokens. A class that does not exist has no values to judge.
 *
 * A class counts as defined only where it appears as a base — `.x`, `.x:hover`,
 * `.parent .x`. `.x.is-active` does NOT define it: a modifier that never had a
 * base is exactly the shape `.session-action` had.
 *
 * Only the class-bearing case is reported. A `<button>` with no className is
 * usually reached by a container's element selector (`.message-actions button`)
 * — a different defect, and not one this regex can tell from a real omission.
 */
const CLASS_IN_SELECTOR_RE = /\.([a-zA-Z][\w-]*)([^\s,{>+~]*)/g;
const baseDefined = new Set();
for (const f of files) {
  if (!f.endsWith('.css')) continue;
  const code = blankComments(readFileSync(f, 'utf8'));
  for (const rule of code.matchAll(RULE_RE)) {
    for (const m of rule[1].matchAll(CLASS_IN_SELECTOR_RE)) {
      // What follows the name decides whether this is a base rule or a
      // modifier. A pseudo-class/element is still a base; another class or an
      // attribute selector means the name only ever appears qualified.
      if (m[2] === '' || m[2].startsWith(':')) baseDefined.add(m[1]);
    }
  }
}

/**
 * PRIVATE-BUTTON check — the control language, declared rather than inferred.
 *
 * The `unstyled-button` pass below catches a class with NO rule. What it
 * cannot see is a control styled privately and CONSISTENTLY: 126 of those sat
 * on `<button>` when the button language covered text actions only, and every
 * other check here passed them, because a private family uses tokens
 * perfectly and simply exists twice.
 *
 * So the rule is not "is it styled" but "is it one of ours". Every `<button>`
 * carries a class from the list below, and the list IS the app's set of
 * controls. Adding to it is a deliberate act that shows up in a diff — which
 * is the whole difference between a library and a habit.
 */
const CONTROL_CLASSES = {
  // The library — components/common, each one the app's only version of what
  // it is.
  btn: 'Button',
  'icon-btn': 'IconButton',
  'close-btn-inline': 'CloseButton',
  chip: 'Chip',
  'menu-item': 'MenuItem',
  disclosure: 'Disclosure',
  segment: 'Segmented',
  switch: 'Switch',
  scrim: 'Scrim',
  'edge-tab': 'EdgeTab',
  'composer-btn': 'ComposerButton',
  'nav-tab': 'Tabs',
  'nav-chip': 'Chips',
  'nav-rail__row': 'NavRail',
  // A shared component's own internals. Not the general language, but not
  // private either: one component owns the class and every surface gets it by
  // rendering that component.
  'md-copy': 'Markdown',
  'md-expand': 'Markdown',
  'cadence-cell-step': 'IntervalCell',
  toast: 'ToastStack',
  // The cockpit HUD, whose bar has a footprint of its own that nothing else
  // shares. Each of these is one control in one file; they are listed so that
  // a SECOND surface reaching for one has to come here and say so.
  'hud-tab': 'BottomHud',
  'hud-mic': 'HudMicButton',
  'hud-voice-mode': 'HudMicButton',
  'hud-sessions': 'ChatHudGroup',
  'hud-chat-toggle': 'HudChatInput',
  'top-status-hud__update': 'TopStatusHud',
  'top-status-hud__activity': 'TopStatusHud',
  'top-status-hud__name': 'AssistantMenu',
};

/**
 * ROW check — the clickable non-buttons.
 *
 * `private-button` reads `<button>` and nothing else, so it saw none of the 26
 * `<div>`/`<li>`/`<span>` elements that carried an `onClick`. They were the
 * same control the button language already covers, wearing a tag the gate had
 * no opinion about — and they disagreed about whether they could be used at
 * all: the autonomy rows shipped `role`, `tabIndex` and an Enter/Space
 * handler, the terminal tabs shipped none of the three and could not be
 * reached by keyboard.
 *
 * A row that can hold its own controls is `Row`; the slot those controls sit
 * in is `RowActions`. Both emit the class below, so a surface passes this
 * check by rendering the component. `<a>` is NOT in the tag list: a link
 * navigates, and the browser already gives it focus, Enter, and a context
 * menu — making it a button would take those away.
 */
const ROW_CLASSES = {
  row: 'Row',
  row__actions: 'RowActions',
};

const CLICKABLE_TAG_RE =
  /<(div|li|span|section|article|header|footer|nav|aside|p|ul|ol|tr|td|label|img|svg)\b([\s\S]*?)>/g;

/**
 * FIELD check — the raw form elements.
 *
 * The four field components existed and were shared by CONVENTION only: 36
 * `<input>`/`<select>`/`<textarea>` elements bypassed them, and what they lost
 * was not tidiness. Seven had no stylesheet reaching them at all and rendered
 * as white browser boxes in a near-black app; two sliders came out in the
 * browser's blue because their surface forgot `accent-color`; and eleven
 * carried no accessible name.
 *
 * The list below is the app's set of field kinds, the same way
 * `CONTROL_CLASSES` is its set of controls. The last four are ONE field in ONE
 * file each — three composers and the schedule stepper's own digits — and they
 * are listed rather than exempted so that a second surface reaching for one
 * has to come here and say so.
 */
const FIELD_CLASSES = {
  input: 'Input',
  textarea: 'Textarea',
  select: 'Select',
  checkbox__box: 'Checkbox',
  radio__box: 'Radio',
  range: 'Range',
  'color-well': 'ColorWell',
  'file-trigger': 'FileTrigger',
  'nav-rail__input': 'NavRail',
  'chat-input': 'ChatInput',
  'hud-chat-input-field': 'HudChatInput',
  'term-command-input': 'CommandBar',
  'cadence-cell-input': 'IntervalCell',
};

const FIELD_TAG_RE = /<(input|select|textarea)\b([\s\S]*?)>/g;

/**
 * MARKDOWN check — the ONE renderer.
 *
 * The surfaces (`Block`, `Note`, `Hint`, `ViewHeader`, `RailView`, `Markdown`)
 * are shared by convention, and mostly cannot be gated: a `Block` is a `<div>`,
 * and so are the other 792 in this tree. `Markdown` is the exception, because a
 * second one cannot be written without reaching for a library — so THAT is the
 * tripwire, and it is as exact as `<button>` was.
 *
 * The two are not the same defect. A second card is drift. A second markdown
 * renderer is a second sanitiser policy, a second link handler and a second
 * answer to what a fenced code block does — `lib/linkify.tsx` exists precisely
 * because machine text must NOT be interpreted, and that distinction only holds
 * while one component owns it.
 */
const MARKDOWN_IMPORT_RE =
  /^\s*import\s[\s\S]*?from\s*["'](react-markdown|marked|markdown-it|remark[\w-]*|micromark[\w-]*|snarkdown|showdown)["']/gm;
const MARKDOWN_HOME = 'Markdown.tsx';

/**
 * RAW-HTML check. `dangerouslySetInnerHTML` is the other way to grow a second
 * renderer, and the one that skips the sanitiser entirely.
 *
 * `CodeRenderer` is the declared exception: it injects highlight.js output,
 * which is markup this app generated from source text it already has, not
 * remote content being interpreted.
 */
const RAW_HTML_HOMES = new Set(['CodeRenderer.tsx', 'Markdown.tsx']);

/**
 * MODAL check — `aria-modal` is the tripwire the surfaces mostly lack.
 *
 * A `Block` is a `<div>` and so are the other 792 in this tree, which is why
 * the card surfaces cannot be gated. A modal CAN be: nothing else in the app
 * writes `aria-modal`, so the attribute means what the component means, the
 * way `<button>` did.
 *
 * It found two the clickable sweep could not — `Mode` and `About` each drew a
 * backdrop with no `onClick` on it, so neither closed on Escape nor on a click
 * beside the card, and a measurement that keys on `onClick` sees nothing.
 *
 * `ExpandOverlay` is the one declared exception: a full-bleed preview with its
 * own header and its own scrim level, not a centred panel. A drawer or an
 * ambient panel uses `role="dialog"` WITHOUT `aria-modal` and is not this
 * check's business.
 */
const MODAL_HOMES = new Set(['Modal.tsx', 'ExpandOverlay.tsx']);

/**
 * TITLE check — the native tooltip.
 *
 * `Hint` replaced 87 of these. A native `title=` waits a second, cannot be
 * styled, cannot be reached by touch, and is the browser's tooltip rather than
 * the app's — which is why the ones left behind read as belonging to a
 * different program.
 *
 * Only on a LOWERCASE tag: `<Block title=…>` and `<RailView>`'s section titles
 * are component props that happen to share the name. `<iframe>` is exempt —
 * there `title` is the required accessible name, not a tooltip, and there is no
 * other way to give one.
 *
 * The `(?!=)` is load-bearing: `Block` builds a class with
 * `${title === null ? … }` INSIDE its open tag, and a comparison read as an
 * attribute is the one false positive this check produced.
 */
const TITLE_ATTR_RE = /<([a-z][a-z0-9]*)\b([^<>]*?)\btitle\s*=(?!=)/g;
const TITLE_EXEMPT_TAGS = new Set(['iframe']);

const BUTTON_OPEN_RE = /<button\b([\s\S]*?)>/g;

/**
 * The comment block immediately above a JSX tag, in either form the tree
 * writes one: `{/* … *​/}` between children, and `// …` where the tag opens a
 * JS expression (`return (`, `createPortal(`). Scoped to what touches the tag
 * rather than a fixed number of lines — a comment long enough to hold a real
 * reason is several lines, and a window sized for a short one silently ignores
 * a long one.
 */
function precedingComment(text, index) {
  const before = text.slice(0, index);
  const block = before.match(/\{?\/\*([\s\S]*?)\*\/\}?\s*$/);
  if (block) return block[1];
  const lines = before.match(/(?:^[ \t]*\/\/[^\n]*\n)+[ \t]*$/m);
  return lines ? lines[0] : '';
}

/**
 * The static class tokens of a `className=` attribute.
 *
 * A regex cannot read these: a shared component writes
 * `` className={`btn${active ? ' is-active' : ''}`} `` and the interpolation
 * holds a template literal of its own, so any `[^`]*` stops at the wrong
 * backtick and the whole attribute reads as empty — which is how the library's
 * own components came out as unclassed the first time this ran. Scanning with
 * a depth counter is the only honest way to skip an expression whatever is
 * nested inside it.
 */
function classTokens(attrs) {
  const at = attrs.indexOf('className=');
  if (at === -1) return [];
  let i = at + 'className='.length;
  if (attrs[i] === '"') {
    const end = attrs.indexOf('"', i + 1);
    if (end === -1) return [];
    return attrs.slice(i + 1, end).split(/\s+/).filter(Boolean);
  }
  if (attrs[i] !== '{') return [];
  let depth = 0;
  let literal = '';
  let inExpr = 0;
  for (; i < attrs.length; i++) {
    const c = attrs[i];
    if (c === '{') {
      depth++;
      // `${` opens an interpolation; everything inside is runtime-chosen.
      if (depth > 1 || attrs[i - 1] === '$') inExpr++;
      continue;
    }
    if (c === '}') {
      depth--;
      if (inExpr > 0) inExpr--;
      if (depth === 0) break;
      continue;
    }
    // The `$` of a `${` belongs to the interpolation, not to the class before
    // it — kept, it welds itself onto the name and `btn$` matches nothing.
    if (inExpr === 0 && c !== '`' && !(c === '$' && attrs[i + 1] === '{')) {
      literal += c;
    }
  }
  return literal.split(/\s+/).filter((c) => /^[a-zA-Z][\w-]*$/.test(c));
}

for (const f of files) {
  if (!/\.tsx$/.test(f) || /\.test\.tsx$/.test(f)) continue;
  const text = readFileSync(f, 'utf8');
  const srcLines = text.split('\n');
  // A tag NAMED in a comment is not a tag. Every one of these components
  // documents itself by describing the element it replaces — "it is a
  // `<button>`", "the only form `<input type="color">` accepts" — and the
  // first run of these two checks reported all six of those docstrings.
  const lineOf = (index) => text.slice(0, index).split('\n').length;
  const inComment = (line) => isCommentLine(srcLines[line - 1] ?? '');

  // A tag carrying an `onClick` that is not one of the shared row components.
  for (const m of text.matchAll(CLICKABLE_TAG_RE)) {
    if (!/\bonClick\s*=/.test(m[2])) continue;
    const line = lineOf(m.index);
    if (inComment(line)) continue;
    const tokens = classTokens(m[2]);
    if (tokens.some((c) => c in ROW_CLASSES)) continue;
    if (/brand-exempt:/.test(precedingComment(text, m.index))) continue;
    allViolations.push({
      file: f,
      line,
      type: 'clickable-non-button',
      value: `<${m[1]}> ${tokens.join(' ') || '(no class)'}`,
      text: `<${m[1]} className="${tokens.join(' ')}" onClick={…}>`,
    });
  }

  // A second markdown renderer, or a way around the one we have.
  const base = f.split(/[\\/]/).pop();
  if (base !== MARKDOWN_HOME) {
    for (const m of text.matchAll(MARKDOWN_IMPORT_RE)) {
      allViolations.push({
        file: f,
        line: lineOf(m.index),
        type: 'second-markdown',
        value: m[1],
        text: m[0].replace(/\s+/g, ' ').trim().slice(0, 90),
      });
    }
  }
  if (!RAW_HTML_HOMES.has(base)) {
    for (const m of text.matchAll(/dangerouslySetInnerHTML/g)) {
      const line = lineOf(m.index);
      if (inComment(line)) continue;
      allViolations.push({
        file: f, line, type: 'raw-html',
        value: 'dangerouslySetInnerHTML',
        text: (srcLines[line - 1] ?? '').trim().slice(0, 90),
      });
    }
  }

  if (!MODAL_HOMES.has(base)) {
    for (const m of text.matchAll(/\baria-modal\s*=/g)) {
      const line = lineOf(m.index);
      if (inComment(line)) continue;
      if (/brand-exempt:/.test(precedingComment(text, m.index))) continue;
      allViolations.push({
        file: f, line, type: 'hand-rolled-modal',
        value: 'aria-modal',
        text: (srcLines[line - 1] ?? '').trim().slice(0, 90),
      });
    }
  }

  // The browser's tooltip where the app has its own.
  for (const m of text.matchAll(TITLE_ATTR_RE)) {
    if (TITLE_EXEMPT_TAGS.has(m[1])) continue;
    const line = lineOf(m.index);
    if (inComment(line)) continue;
    if (/brand-exempt:/.test(precedingComment(text, m.index))) continue;
    allViolations.push({
      file: f, line, type: 'native-tooltip',
      value: `<${m[1]} title=…>`,
      text: (srcLines[line - 1] ?? '').trim().slice(0, 90),
    });
  }

  // A form element that is not one of the shared fields.
  for (const m of text.matchAll(FIELD_TAG_RE)) {
    const line = lineOf(m.index);
    if (inComment(line)) continue;
    const tokens = classTokens(m[2]);
    if (tokens.some((c) => c in FIELD_CLASSES)) continue;
    if (/brand-exempt:/.test(precedingComment(text, m.index))) continue;
    allViolations.push({
      file: f,
      line,
      type: 'raw-field',
      value: `<${m[1]}> ${tokens.join(' ') || '(no class)'}`,
      text: `<${m[1]} className="${tokens.join(' ')}">`,
    });
  }

  for (const m of text.matchAll(BUTTON_OPEN_RE)) {
    const line = lineOf(m.index);
    if (inComment(line)) continue;
    const tokens = classTokens(m[1]);
    // Same escape hatch as the CSS passes.
    if (/brand-exempt:/.test(precedingComment(text, m.index))) continue;

    if (!tokens.some((c) => c in CONTROL_CLASSES)) {
      allViolations.push({
        file: f,
        line,
        type: 'private-button',
        value: tokens.join(' ') || '(no class)',
        text: `<button className="${tokens.join(' ')}">`,
      });
      continue;
    }
    if (tokens.length === 0) continue;
    if (tokens.some((c) => baseDefined.has(c))) continue;
    allViolations.push({
      file: f,
      line,
      type: 'unstyled-button',
      value: tokens.join(' '),
      text: `<button className="${tokens.join(' ')}">`,
    });
  }
}

if (allViolations.length === 0) {
  console.log(
    '[audit:tokens] PASS — no hard-coded hex/rgba/hsl/ms/seconds/font-size, no raw px in padding/margin/gap/border-width/letter-spacing, no stylesheet declaring a brand value of its own, no caller repainting a shared control, every button one of the shared controls, every clickable non-button a Row, every form element one of the shared fields, one markdown renderer, one Modal, no native tooltips, and every var() names a declared token',
  );
  process.exit(0);
} else {
  console.error(`[audit:tokens] FAIL — ${allViolations.length} violation(s) found:\n`);
  // One line per type that actually fired. The primitive-restyle check is the
  // least self-explanatory of the six — its rule uses tokens correctly — so
  // leaving any of them unexplained was worst for exactly that one.
  const HINTS = {
    hex: 'name a token from src/styles/tokens.css instead of the literal',
    'color-literal': 'name a token; alpha variants are the --<colour>-wash/soft/edge/strong/bold ramps',
    ms: 'use --motion-instant/fast/med/slow',
    seconds: 'use --motion-instant/fast/med/slow, or --motion-beat/breathe/swell/ring/drift for an ambient loop',
    'font-size': 'use a type tier (--t-meta/caption/body/ui/sub/head/display-size)',
    'own-token': 'declare the value in tokens.css; a local alias may only FORWARD a token (var(--x))',
    'primitive-restyle': 'the control owns its appearance — move the rule into its own stylesheet, or restyle a wrapper instead. Placement (margin, alignment, grid) is allowed here',
    'undefined-token': 'the property is not declared, so the whole declaration is dropped and the element renders unstyled',
    'unstyled-button': 'no stylesheet gives this class a base rule, so the button renders as raw browser chrome — use the shared `Button` (tone= carries the emphasis) rather than reviving the class',
    'hand-rolled-modal': 'render `Modal` — it supplies the scrim, the stacking order, Escape and a panel layer that needs no stopPropagation. Keep only the panel\'s SIZE in your own class. A drawer or ambient panel uses role="dialog" without aria-modal and is not this check',
    'second-markdown': 'the tree has ONE markdown renderer — `components/common/Markdown.tsx`. A second one is a second sanitiser policy and a second answer to what a fenced code block does. For machine text that must NOT be interpreted (tool results, raw views, pulse lines), `lib/linkify.tsx` is the answer',
    'raw-html': 'injecting markup skips the renderer and its sanitiser — render `Markdown` instead. Only `CodeRenderer` may, and only for highlight.js output over source this app already holds',
    'native-tooltip': 'the browser\'s tooltip is not the app\'s — it waits a second, cannot be styled and never appears on touch. Wrap the element in `Hint` (it is `display: contents`, so it moves no layout). On an <iframe> `title` is the required accessible name and is exempt',
    'clickable-non-button': 'a row you can click is a control — render `Row` (it may hold its own buttons, which `MenuItem` cannot) and put those buttons in a `RowActions`. Row supplies role, tabIndex and Enter/Space, which every hand-rolled one either forgot or spelled differently. If this genuinely is not a control — a focus region, a click the panel merely holds — say so in a `brand-exempt:` comment above the tag',
    'raw-field': 'render one of the shared fields instead — Input (text/number/date/password/search), Textarea, Select, Checkbox, Radio, Range, ColorWell or FileTrigger. A caller className is for WIDTH and placement only. If this is a new KIND of field, add it to FIELD_CLASSES in this script naming the component that owns it',
    'private-button': 'render one of the shared controls instead — Button, IconButton, CloseButton, Chip, MenuItem, Disclosure, Segmented, Switch, Scrim, EdgeTab or ComposerButton. A private class beside a shared one is fine and is for PLACEMENT only. If this genuinely is a new KIND of control, add it to CONTROL_CLASSES in this script naming the component that owns it',
    'space-px': 'use --space-<n>, where n IS the px value (--space-6 is 6px). A negative is calc(var(--space-6) * -1). If the step does not exist, add it to tokens.css — the scale is a census of what the app uses',
    'stroke-px': 'use --stroke-1/2/3/4 for a border or outline WIDTH. border-radius and outline-offset are excluded and are not this check',
    'tracking-px': 'letter-spacing in px does not scale with the operator text-size control — divide by the element\'s own font-size and write it in em',
  };
  for (const type of new Set(allViolations.map((v) => v.type))) {
    if (HINTS[type]) console.error(`  [${type}] ${HINTS[type]}`);
  }
  console.error(
    '\n  A literal that is genuinely required takes `/* brand-exempt: <reason> */`\n' +
    '  on the line ABOVE THE DECLARATION (inside the rule) — a comment above the\n' +
    '  selector is out of scope and will not exempt anything.\n',
  );
  for (const v of allViolations) {
    const rel = relative(join(__dirname, '..'), v.file).replace(/\\/g, '/');
    console.error(`  ${rel}:${v.line}  [${v.type}] ${v.value}`);
    console.error(`    ${v.text}`);
  }
  process.exit(1);
}
