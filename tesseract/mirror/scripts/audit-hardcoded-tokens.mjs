/**
 * audit-hardcoded-tokens.mjs
 *
 * Scans src/**\/*.{ts,tsx,css} and public/**\/*.html for hard-coded values
 * that have canonical token replacements in tokens.css.
 *
 * Flags:
 *   - HEX colors:   #[0-9a-fA-F]{3,8}  (always has a token replacement)
 *   - Raw ms:       \d+ms  (motion tokens exist: --motion-fast/med/slow)
 *
 * NOT flagged (component-specific, below token floor):
 *   - Raw px values — spacing/radius tokens cover common values but many
 *     component-level measurements (font-size, border-width, fixed dimensions)
 *     are legitimately below the token floor. These should be reviewed manually.
 *
 * HTML files (public/**\/*.html) are standalone documents with no access to
 * tokens.css, so only their <style> blocks are scanned — inline <script>
 * bodies and HTML attributes (e.g. <input type="color" value="#...">) are
 * implementation detail, not design surface, and are excluded to avoid
 * false positives.
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

    const line = filePath.endsWith('.css') || isHtml ? rawLine : stripStringLiterals(rawLine);

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
  });

  return violations;
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

if (allViolations.length === 0) {
  console.log('[audit:tokens] PASS — no hard-coded hex/ms found outside tokens.css');
  process.exit(0);
} else {
  console.error(`[audit:tokens] FAIL — ${allViolations.length} violation(s) found:\n`);
  for (const v of allViolations) {
    const rel = relative(join(__dirname, '..'), v.file).replace(/\\/g, '/');
    console.error(`  ${rel}:${v.line}  [${v.type}] ${v.value}`);
    console.error(`    ${v.text}`);
  }
  process.exit(1);
}
