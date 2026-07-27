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

// --- Summary ---
console.log(`\n${'─'.repeat(50)}`);
console.log(`audit-hardcoded-tokens.test: ${passed} passed, ${failed} failed`);
if (failed > 0) {
  process.exit(1);
} else {
  console.log('[audit:tokens:test] PASS — all fixtures behave correctly');
  process.exit(0);
}
