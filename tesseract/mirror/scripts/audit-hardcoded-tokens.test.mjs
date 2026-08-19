/**
 * audit-hardcoded-tokens.test.mjs
 *
 * Proves that audit-hardcoded-tokens.mjs correctly detects violations.
 * Runs via: node scripts/audit-hardcoded-tokens.test.mjs
 * Exits 0 if all assertions pass, 1 on any failure.
 */

import { writeFileSync, mkdirSync, rmSync, existsSync } from 'fs';
import { join } from 'path';
import { fileURLToPath } from 'url';
import { spawnSync } from 'child_process';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const TMP_DIR = join(__dirname, '..', 'src', '__audit_test_tmp__');
const AUDIT_SCRIPT = join(__dirname, 'audit-hardcoded-tokens.mjs');

let passed = 0;
let failed = 0;

function assert(label, condition) {
  if (condition) {
    console.log(`  [PASS] ${label}`);
    passed++;
  } else {
    console.error(`  [FAIL] ${label}`);
    failed++;
  }
}

function runAudit() {
  const result = spawnSync('node', [AUDIT_SCRIPT], { encoding: 'utf-8' });
  return { code: result.status, stdout: result.stdout, stderr: result.stderr };
}

function setup() {
  if (existsSync(TMP_DIR)) rmSync(TMP_DIR, { recursive: true });
  mkdirSync(TMP_DIR, { recursive: true });
}

function teardown() {
  if (existsSync(TMP_DIR)) rmSync(TMP_DIR, { recursive: true });
}

// --- Test 1: clean file — no violations ---
console.log('\nTest 1: clean file exits 0');
setup();
writeFileSync(join(TMP_DIR, 'clean.css'), `
.foo {
  color: var(--text-primary);
  background: var(--bg-surface);
  padding: var(--space-md);
  transition: opacity var(--motion-fast) var(--ease-out);
}
`);
{
  const r = runAudit();
  assert('exit code 0 for clean file', r.code === 0);
  assert('stdout contains PASS', r.stdout.includes('PASS'));
}
teardown();

// --- Test 2: hex in .css triggers exit 1 ---
console.log('\nTest 2: hex color in .css triggers exit 1');
setup();
writeFileSync(join(TMP_DIR, 'hex-violation.css'), `
.bar {
  color: #ff0000;
}
`);
{
  const r = runAudit();
  assert('exit code 1 for hex violation', r.code === 1);
  assert('stderr mentions hex', r.stderr.includes('[hex]'));
  assert('stderr mentions #ff0000', r.stderr.includes('#ff0000'));
}
teardown();

// --- Test 3: raw px in .tsx is NOT flagged (below token floor) ---
console.log('\nTest 3: raw px in .tsx is not flagged (below token floor)');
setup();
writeFileSync(join(TMP_DIR, 'px-clean.tsx'), `
export function Foo() {
  return <div style={{ width: '360px' }} />;
}
`);
{
  const r = runAudit();
  assert('exit code 0 — px not flagged (below token floor)', r.code === 0);
  assert('stdout contains PASS', r.stdout.includes('PASS'));
}
teardown();

// --- Test 4: raw ms in .ts triggers exit 1 ---
console.log('\nTest 4: raw ms in .ts triggers exit 1');
setup();
writeFileSync(join(TMP_DIR, 'ms-violation.ts'), `
const TIMEOUT = 250ms;
`);
{
  const r = runAudit();
  assert('exit code 1 for ms violation', r.code === 1);
  assert('stderr mentions ms', r.stderr.includes('[ms]'));
}
teardown();

// --- Test 5: comment lines are not flagged ---
console.log('\nTest 5: comment lines are not flagged');
setup();
writeFileSync(join(TMP_DIR, 'comments.css'), `
/* was: color: #aabbcc; */
// color: 250ms;
.ok { color: var(--text-primary); }
`);
{
  const r = runAudit();
  assert('exit code 0 — comments not flagged', r.code === 0);
}
teardown();

// --- Test 6: multiple violations (hex + ms) are all reported; px is silent ---
console.log('\nTest 6: multiple violations (hex + ms) all reported, px silent');
setup();
writeFileSync(join(TMP_DIR, 'multi.css'), `
.a { color: #123456; }
.b { transition: 200ms; }
.c { margin: 8px; }
`);
{
  const r = runAudit();
  assert('exit code 1', r.code === 1);
  assert('reports hex', r.stderr.includes('[hex]'));
  assert('reports ms', r.stderr.includes('[ms]'));
  assert('px is not reported', !r.stderr.includes('[px]'));
}
teardown();

// --- Test 7: a colour built from digits is flagged; the same function
//     reading a token is not ---
console.log('\nTest 7: colour literals flagged, token-reading colour functions not');
setup();
writeFileSync(join(TMP_DIR, 'colors.css'), `
.literal { background: rgba(255, 180, 84, 0.12); }
.also    { border-color: hsl(210 80% 50% / 0.07); }
.correct { background: hsl(var(--accent-hsl) / 0.15); }
.mixed   { background: color-mix(in srgb, var(--ok) 12%, transparent); }
`);
{
  const r = runAudit();
  assert('exit code 1', r.code === 1);
  assert('reports the rgba literal', r.stderr.includes('rgba(255, 180, 84, 0.12)'));
  assert('reports the hsl literal', r.stderr.includes('hsl(210 80% 50% / 0.07)'));
  assert('hsl(var(--token)) is not reported', !r.stderr.includes('accent-hsl'));
  assert('color-mix is not reported', !r.stderr.includes('color-mix'));
}
teardown();

// --- Test 8: a duration in seconds is flagged, and only on a motion line ---
console.log('\nTest 8: second-form durations flagged on transition/animation only');
setup();
writeFileSync(join(TMP_DIR, 'seconds.css'), `
.a { transition: opacity 0.12s ease; }
.b { animation: spin 26s linear infinite; }
.c { transition: opacity var(--motion-fast) ease; }
.d { grid-template-areas: "s"; }
`);
{
  const r = runAudit();
  assert('exit code 1', r.code === 1);
  assert('reports the transition seconds', r.stderr.includes('[seconds] 0.12s'));
  assert('reports the animation seconds', r.stderr.includes('[seconds] 26s'));
  assert('a token duration is not reported', !r.stderr.includes('motion-fast'));
}
teardown();

// --- Test 8b: the multiline form, which the first version of this check
//     missed entirely — two live `0.22s` values sat behind it and the audit
//     reported PASS (Trio auditor + consistency lenses, 2026-08-14) ---
console.log('\nTest 8b: seconds inside a multiline transition declaration');
setup();
writeFileSync(join(TMP_DIR, 'multiline.css'), `
.a {
  transition:
    transform 0.22s ease,
    opacity 0.22s ease;
}
.b {
  transition:
    transform var(--motion-med) ease,
    opacity var(--motion-med) ease;
}
`);
{
  const r = runAudit();
  assert('exit code 1', r.code === 1);
  assert('reports both seconds values', (r.stderr.match(/\[seconds\] 0\.22s/g) || []).length === 2);
  assert('the tokenised declaration is not reported', !r.stderr.includes('motion-med'));
}
teardown();

// --- Test 9: brand-exempt lets a named literal through, and only that one ---
console.log('\nTest 9: brand-exempt honoured, and scoped to its declaration');
setup();
writeFileSync(join(TMP_DIR, 'exempt.css'), `
.hue {
  /* brand-exempt: this control renders the hue axis. */
  background: linear-gradient(
    hsl(0 80% 50%),
    hsl(180 80% 50%)
  );
  border-color: rgba(255, 0, 0, 0.5);
}
`);
{
  const r = runAudit();
  assert('exit code 1 — the un-exempted literal still fails', r.code === 1);
  assert('the exempted gradient is not reported', !r.stderr.includes('hsl(180 80% 50%)'));
  assert('the next declaration is reported', r.stderr.includes('rgba(255, 0, 0, 0.5)'));
}
teardown();

// --- Test 10: a caller restyling a shared primitive from another file ---
console.log('\nTest 10: cross-file restyle of a shared control');
setup();
writeFileSync(join(TMP_DIR, 'restyle.css'), `
.cost-row .btn { background: var(--bg-card); }
.cost-row .checkbox__box { margin-right: var(--space-xs); vertical-align: middle; }
.cost-row .input-wide { background: var(--bg-card); }
`);
{
  const r = runAudit();
  assert('exit code 1', r.code === 1);
  assert('reports the repainted .btn', r.stderr.includes('primitive-restyle'));
  assert('reports it against its owner', r.stderr.includes('owned by ui.css'));
  assert('placement-only rule is not reported', !r.stderr.includes('checkbox__box'));
  assert('a different class with the same prefix is not reported', !r.stderr.includes('input-wide'));
}
teardown();

// --- Test 11: a colour function written across lines (Trio auditor lens,
//     pass 2 — the same shape that defeated the seconds check) ---
console.log('\nTest 11: colour literal split across lines');
setup();
writeFileSync(join(TMP_DIR, 'wrapped.css'), `
.a {
  background: rgba(
    255, 180, 84,
    0.12
  );
}
`);
{
  const r = runAudit();
  assert('exit code 1', r.code === 1);
  assert('reports the wrapped literal', r.stderr.includes('[color-literal]'));
}
teardown();

// --- Test 12: a token declared in one file does not vouch for another
//     (Trio consistency lens, pass 2) ---
console.log('\nTest 12: local declarations do not leak between files');
setup();
writeFileSync(join(TMP_DIR, 'a-declares.css'), `
.a { --only-here: var(--accent); color: var(--only-here); }
`);
writeFileSync(join(TMP_DIR, 'b-borrows.css'), `
.b { color: var(--only-here); }
`);
{
  const r = runAudit();
  assert('exit code 1', r.code === 1);
  assert('the borrowing file is reported', r.stderr.includes('b-borrows.css'));
  assert('the declaring file is not', !r.stderr.includes('a-declares.css'));
}
teardown();

// --- Test 13: a stylesheet may forward a token but not invent one ---
console.log('\nTest 13: own-token check allows a forwarding alias only');
setup();
writeFileSync(join(TMP_DIR, 'own.css'), `
.ok  { --swatch-h: var(--accent-h); }
.bad { --my-blue: #3366ff; }
`);
{
  const r = runAudit();
  assert('exit code 1', r.code === 1);
  assert('reports the invented value', r.stderr.includes('[own-token] --my-blue'));
  assert('the forwarding alias is not reported', !r.stderr.includes('swatch-h'));
}
teardown();

// --- Test 14: a commented-out value is not a violation, on any of the
//     whole-file passes (they do not walk lines, so isCommentLine cannot
//     help them) ---
console.log('\nTest 14: commented-out values are not flagged by the whole-file passes');
setup();
writeFileSync(join(TMP_DIR, 'commented.css'), `
.a {
  /* was: background: rgba(255, 180, 84, 0.12); */
  background: var(--warn-soft);
  /* was: transition: opacity 0.2s ease; */
  transition: opacity var(--motion-fast) ease;
  /* was: --my-blue: #3366ff; */
}
/* .cost-row .btn { background: var(--bg-card); } */
`);
{
  const r = runAudit();
  assert('exit code 0 — nothing in a comment is a violation', r.code === 0);
}
teardown();

// --- Test 15: a colour in a TSX inline style, which stripStringLiterals used
//     to blank before the check could see it (Trio auditor lens, pass 3) ---
console.log('\nTest 15: colour literal in a TSX inline style');
setup();
writeFileSync(join(TMP_DIR, 'inline.tsx'), `
export function Foo() {
  return <div style={{ color: 'rgba(255, 0, 0, 0.5)' }} className="text-thing" />;
}
`);
{
  const r = runAudit();
  assert('exit code 1', r.code === 1);
  assert('reports the inline colour', r.stderr.includes('rgba(255, 0, 0, 0.5)'));
}
teardown();

// --- Test 16: the space scale bites on each of the five property groups ---
console.log('\nTest 16: raw px in padding/margin/gap/border-width/letter-spacing');
setup();
writeFileSync(join(TMP_DIR, 'space-violation.css'), `
.a { padding: 6px; }
.b { padding-left: 10px; }
.c { margin: 0 12px; }
.d { margin-bottom: 3px; }
.e { gap: 5px; }
.f { row-gap: 7px; }
.g { column-gap: 9px; }
.h { border: 1px solid var(--border); }
.i { border-bottom-width: 2px; }
.j { outline: 2px solid var(--accent); }
.k { letter-spacing: 0.5px; }
`);
{
  const r = runAudit();
  assert('exit code 1', r.code === 1);
  assert('padding flagged', r.stderr.includes('[space-px] 6px'));
  assert('padding longhand flagged', r.stderr.includes('[space-px] 10px'));
  assert('margin shorthand flagged', r.stderr.includes('[space-px] 12px'));
  assert('margin longhand flagged', r.stderr.includes('[space-px] 3px'));
  assert('gap flagged', r.stderr.includes('[space-px] 5px'));
  assert('row-gap flagged', r.stderr.includes('[space-px] 7px'));
  assert('column-gap flagged', r.stderr.includes('[space-px] 9px'));
  assert('border shorthand width flagged', r.stderr.includes('[stroke-px] 1px'));
  assert('border-*-width flagged', r.stderr.includes('[stroke-px] 2px'));
  assert('letter-spacing flagged', r.stderr.includes('[tracking-px] 0.5px'));
}
teardown();

// --- Test 17: and does NOT over-reach past those five ---
//     Every property here is a per-component MEASUREMENT. If this fixture ever
//     goes red the audit has become unsatisfiable, not stricter.
console.log('\nTest 17: excluded properties keep their px');
setup();
writeFileSync(join(TMP_DIR, 'space-clean.css'), `
.rail { width: 176px; min-width: 140px; max-height: 480px; }
.pos { position: absolute; top: 7px; right: 13px; bottom: 3px; left: 11px; }
.ins { inset: 2px; }
.grid { grid-template-columns: 176px 1fr; grid-auto-rows: 22px; }
.shadow { box-shadow: 0 6px 18px var(--wash-sm); }
.round { border-radius: 999px; }
.focus { outline-offset: 3px; }
.ok { padding: var(--space-6) var(--space-12); gap: var(--space-4); }
.neg { margin-top: calc(var(--space-6) * -1); }
.stroke { border: var(--stroke-1) solid var(--border); }
.track { letter-spacing: 0.05em; }
`);
{
  const r = runAudit();
  assert('exit code 0 — measurements are not space', r.code === 0);
  assert('stdout contains PASS', r.stdout.includes('PASS'));
}
teardown();

// --- Test 18: a px shorthand split across lines is still seen ---
//     The shape that defeated the seconds check before it was rewritten
//     per-declaration: the property name and the value are on different lines.
console.log('\nTest 18: multiline padding shorthand');
setup();
writeFileSync(join(TMP_DIR, 'multiline-space.css'), `
.foo {
  padding:
    var(--space-4)
    18px;
}
`);
{
  const r = runAudit();
  assert('exit code 1', r.code === 1);
  assert('reports the px on the value line', r.stderr.includes('[space-px] 18px'));
}
teardown();

// --- Test 19: brand-exempt reaches the space check too ---
console.log('\nTest 19: brand-exempt on a space declaration');
setup();
writeFileSync(join(TMP_DIR, 'space-exempt.css'), `
.foo {
  /* brand-exempt: optical alignment against a 1px hairline, not a space step */
  padding-top: 9px;
  padding-bottom: 11px;
}
`);
{
  const r = runAudit();
  assert('exit code 1 — the exemption is scoped to its declaration', r.code === 1);
  assert('the exempted value is not reported', !r.stderr.includes('[space-px] 9px'));
  assert('the next declaration still is', r.stderr.includes('[space-px] 11px'));
}
teardown();

// --- Test 20: a private control on a <button> ---
console.log('\nTest 20: a <button> that is not one of the shared controls');
setup();
writeFileSync(
  join(TMP_DIR, 'private-button.tsx'),
  [
    'export function Panel() {',
    '  return (',
    '    <div>',
    '      <button type="button" className="panel-refresh">refresh</button>',
    '      <button type="button" className={`btn${on ? " is-active" : ""} panel-slot`}>ok</button>',
    '      <button type="button">bare</button>',
    '    </div>',
    '  );',
    '}',
  ].join('\n'),
);
writeFileSync(join(TMP_DIR, 'private-button.css'), `
.panel-refresh {
  color: var(--text-meta);
}
`);
{
  const r = runAudit();
  assert('exit code 1', r.code === 1);
  // The point of this check: the class HAS a rule, and every value in it is a
  // token. Only the control language can see that it is a second button.
  assert(
    'a private class is reported even though it has a rule',
    r.stderr.includes('[private-button] panel-refresh'),
  );
  assert('a bare <button> is reported', r.stderr.includes('[private-button] (no class)'));
  assert(
    'a shared control with a placement class beside it passes',
    !r.stderr.includes('[private-button] btn'),
  );
}
teardown();

// --- Test 21: brand-exempt covers a button too ---
console.log('\nTest 21: brand-exempt above a <button>');
setup();
writeFileSync(
  join(TMP_DIR, 'exempt-button.tsx'),
  [
    'export function Panel() {',
    '  return (',
    '    <div>',
    '      {/* brand-exempt: a native control with no shared form */}',
    '      <button type="button" className="panel-refresh">refresh</button>',
    '    </div>',
    '  );',
    '}',
  ].join('\n'),
);
{
  const r = runAudit();
  assert('exit code 0 — the exemption is honoured', r.code === 0);
}
teardown();

// --- Summary ---
console.log(`\n${'─'.repeat(50)}`);
console.log(`audit-hardcoded-tokens.test: ${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
} else {
  console.log('[audit:tokens:test] PASS — all fixtures behave correctly');
  process.exit(0);
}
