/**
 * audit-agent-name.test.mjs
 *
 * Proves audit-agent-name.mjs flags what it should and exempts what it
 * should — a gate that silently misses a violation reads as a pass.
 * Runs via: node scripts/audit-agent-name.test.mjs
 * Exits 0 if all assertions pass, 1 on any failure.
 */

import { writeFileSync, mkdirSync, rmSync, existsSync } from 'fs';
import { join } from 'path';
import { fileURLToPath } from 'url';
import { spawnSync } from 'child_process';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const TMP_DIR = join(__dirname, '..', 'src', '__name_audit_test_tmp__');
const AUDIT_SCRIPT = join(__dirname, 'audit-agent-name.mjs');

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
  const r = spawnSync('node', [AUDIT_SCRIPT], { encoding: 'utf-8' });
  return { code: r.status, stdout: r.stdout, stderr: r.stderr };
}

function withFile(name, body, fn) {
  if (existsSync(TMP_DIR)) rmSync(TMP_DIR, { recursive: true });
  mkdirSync(TMP_DIR, { recursive: true });
  writeFileSync(join(TMP_DIR, name), body);
  try {
    fn(runAudit());
  } finally {
    rmSync(TMP_DIR, { recursive: true });
  }
}

console.log('\nTest 1: a rendered string naming the agent fails');
withFile('bad.tsx', 'export const L = <span>Ask TARS</span>;\n', (r) => {
  assert('exit code 1', r.code === 1);
  assert('names the file', r.stderr.includes('bad.tsx'));
});

console.log('\nTest 2: the name in a comment is exempt');
withFile('ok.tsx', '// TARS used to be hardcoded here\nexport const L = 1;\n', (r) => {
  assert('exit code 0', r.code === 0);
});

console.log('\nTest 3: a multi-line JSX block comment is exempt on every line');
withFile(
  'block.tsx',
  'const x = (\n  {/* captions of\n      what TARS said\n      under the orb */}\n);\n',
  (r) => assert('exit code 0', r.code === 0),
);

console.log('\nTest 4: a trailing comment after code is exempt');
withFile('trail.ts', 'export const m = 1; // TARS stays silent until asked\n', (r) =>
  assert('exit code 0', r.code === 0),
);

console.log('\nTest 5: TESSERACT is not the agent name');
withFile('runtime.tsx', 'export const L = <span>Restart TESSERACT</span>;\n', (r) =>
  assert('exit code 0', r.code === 0),
);

console.log('\nTest 6: a comment marker inside a string does not hide a violation');
withFile('sneaky.ts', 'export const s = "path/* TARS lives here */more";\n', (r) => {
  assert('exit code 1', r.code === 1);
  assert('names the file', r.stderr.includes('sneaky.ts'));
});

console.log('\nTest 7: a // inside a template literal does not hide a violation');
withFile('tpl.ts', 'export const s = `caption // TARS fallback`;\n', (r) => {
  assert('exit code 1', r.code === 1);
});

console.log('\nTest 8: a URL in a string does not swallow the rest of the line');
withFile('url.ts', 'export const s = "https://example.com" + label;\nexport const t = 2;\n', (r) =>
  assert('exit code 0', r.code === 0),
);

console.log('\nTest 9: an escape sequence does not hide the name behind its letter');
withFile('esc.ts', 'export const s = "\\nTARS is not available";\n', (r) => {
  assert('exit code 1', r.code === 1);
});

console.log('\nTest 10: .test.tsx files are exempt — they must name what they refuse');
withFile('thing.test.tsx', 'expect(hint).not.toContain("TARS");\n', (r) =>
  assert('exit code 0', r.code === 0),
);

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
