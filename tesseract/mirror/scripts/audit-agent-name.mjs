/**
 * audit-agent-name.mjs
 *
 * AS-4 — the agent's name is operator-settable config
 * (`mirror.yaml::identity.name`, written through `POST /api/identity`).
 * A rendered string that hardcodes one is a rename that visibly fails to
 * take, which is worse than never offering the rename.
 *
 * Scans src/**\/*.{ts,tsx} for the literal name in anything the operator
 * can read: JSX text, attributes, and string/template literals.
 *
 * NOT flagged:
 *   - Comment lines — they document history and carry no pixels.
 *   - `.test.ts(x)` — a test asserting "this string is not the default" has to
 *     name the string it is refusing.
 *
 * Components read the name with `useEntityName()`; a store reads
 * `useIdentityStore.getState().name` and falls back to `ENTITY_FALLBACK`.
 *
 * Exits 1 on any violation.
 *
 * Run: node scripts/audit-agent-name.mjs
 */

import { readFileSync, readdirSync, statSync } from 'fs';
import { join, relative } from 'path';
import { fileURLToPath } from 'url';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const SRC_DIR = join(__dirname, '..', 'src');

// The shipped default from `mirror.yaml::identity.name`. It used to be the
// persona name; that name is retired, and this gate follows the value rather
// than the word it happened to hold — otherwise it scans for a string nothing
// can produce and passes forever.
//
// Word-bounded and case-sensitive, which is what keeps it quiet: TESSERACT is
// the runtime and is not renameable, `role === 'assistant'` is a protocol
// value, `ENTITY_FALLBACK = 'the assistant'` is lowercase, and an identifier
// like `AssistantBubble` has no boundary after the match.
const NAME_RE = /\bAssistant\b/g;

function collectFiles(dir) {
  const results = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      results.push(...collectFiles(full));
    } else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) {
      results.push(full);
    }
  }
  return results;
}

/**
 * Blank out comments so only rendered text is left.
 *
 * A character scan rather than a regex strip, because a comment marker is
 * only a comment outside a string. `"path/* here *\/more"` and
 * `` `caption // note` `` are both ordinary strings, and stripping them as
 * comments would hide any name inside — a gate that silently misses a
 * violation is worse than no gate.
 *
 * Block state carries across lines: a `{/* … *\/}` caption spanning three
 * lines is a comment on all three, and matching only the opening line is
 * how a comment reads as a violation. String state does not carry — an
 * unterminated quote is a syntax error, not something to model.
 *
 * String CONTENT is kept: that is where rendered text lives and the whole
 * point of the gate. `https://…` survives because the `//` test only runs
 * outside a quote, which is stronger than the whitespace rule it replaces.
 * Escape sequences are dropped rather than copied, so `"\nAssistant"` presents
 * `Assistant` at a word boundary instead of hiding behind the escape's `n`.
 */
function stripComments(lines) {
  let inBlock = false;
  return lines.map((raw) => {
    let out = '';
    let quote = null; // `"`, `'` or a backtick while inside a string
    let i = 0;
    while (i < raw.length) {
      const ch = raw[i];
      const next = raw[i + 1];
      if (inBlock) {
        if (ch === '*' && next === '/') {
          inBlock = false;
          i += 2;
        } else {
          i += 1;
        }
        continue;
      }
      if (quote !== null) {
        if (ch === '\\') {
          i += 2; // escaped char cannot close the string
          continue;
        }
        if (ch === quote) quote = null;
        out += ch;
        i += 1;
        continue;
      }
      if (ch === '/' && next === '/') break; // rest of the line is a comment
      if (ch === '/' && next === '*') {
        inBlock = true;
        i += 2;
        continue;
      }
      if (ch === '"' || ch === "'" || ch === '`') quote = ch;
      out += ch;
      i += 1;
    }
    return out;
  });
}

function scanFile(filePath) {
  const violations = [];
  // Split on CRLF too: a trailing `\r` sits between `.*` and `$`, and the
  // comment strip below would silently match nothing on a Windows checkout.
  const lines = readFileSync(filePath, 'utf-8').split(/\r?\n/);
  stripComments(lines).forEach((line, idx) => {
    for (const m of line.matchAll(NAME_RE)) {
      violations.push({
        file: filePath,
        line: idx + 1,
        value: m[0],
        text: lines[idx].trim(),
      });
    }
  });
  return violations;
}

const violations = collectFiles(SRC_DIR).flatMap(scanFile);

if (violations.length === 0) {
  console.log('[audit:name] PASS — no rendered string hardcodes the agent name');
  process.exit(0);
}

console.error(`[audit:name] FAIL — ${violations.length} violation(s):\n`);
for (const v of violations) {
  const rel = relative(join(__dirname, '..'), v.file).replace(/\\/g, '/');
  console.error(`  ${rel}:${v.line}  ${v.value}`);
  console.error(`    ${v.text}`);
}
console.error('\n  Components: useEntityName(). Stores: useIdentityStore.getState().name || ENTITY_FALLBACK.');
process.exit(1);
