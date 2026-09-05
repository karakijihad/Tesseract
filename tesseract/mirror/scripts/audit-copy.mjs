#!/usr/bin/env node
// Copy carries no dashes.
//
// `—` and `–` were the house tic: 250 strings joined two clauses with one, and
// the reader had to work out which of "namely", "because", "except" or "and
// then" was meant. Comments keep theirs, because a comment is written for
// whoever maintains the file, not for the person in front of the screen.
//
// Crude by design. It strips comments and reports every dash left in the file,
// which over-reports on the rare dash inside an identifier and never
// under-reports on a string that reaches a surface. A dash that genuinely has
// to stay carries `copy-exempt: <reason>` on the same line, and the reason has
// to be in the file.

import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const ROOT = new URL("..", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const SRC = join(ROOT, "src");
// The character, and the two HTML entities that render as it. Six strings
// spelled it `&mdash;` and slipped straight past a check that only knew the
// character.
const DASHES = /[—–]|&mdash;|&ndash;/;
const EXEMPT = /copy-exempt:\s*\S/;

/** Everything outside a `//` line comment or a `/* *​/` block, with the
 *  comment bytes replaced by spaces so line and column numbers survive. */
function stripComments(text) {
  let out = "";
  let i = 0;
  while (i < text.length) {
    const two = text.slice(i, i + 2);
    if (two === "//") {
      const end = text.indexOf("\n", i);
      const stop = end < 0 ? text.length : end;
      out += " ".repeat(stop - i);
      i = stop;
    } else if (two === "/*") {
      const end = text.indexOf("*/", i + 2);
      const stop = end < 0 ? text.length : end + 2;
      out += text.slice(i, stop).replace(/[^\n]/g, " ");
      i = stop;
    } else {
      out += text[i];
      i += 1;
    }
  }
  return out;
}

function* walk(dir) {
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) yield* walk(path);
    else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)) yield path;
  }
}

// A dash that IS the whole string is the app's "no value" mark, not prose:
// `'—'`, `"—"`, `` `—` ``, and the JSX form `{'—'}`. It reads as an empty cell
// and there is nothing in it for a reader to resolve, which is the whole
// complaint against the dash everywhere else. Anything with a word beside it
// is a sentence and is caught.
// Two spellings of the same thing: quoted (`'—'`) and as a bare JSX text node
// with nothing else in the element (`<span className="t-meta">—</span>`).
const NULL_GLYPH = /(['"`])\s*[—–]\s*\1|>\s*[—–]\s*</g;

const findings = [];
for (const path of walk(SRC)) {
  const raw = readFileSync(path, "utf8");
  const rawLines = raw.split("\n");
  stripComments(raw)
    .split("\n")
    .forEach((line, k) => {
      const prose = line.replace(NULL_GLYPH, '>x<');
      if (!DASHES.test(prose)) return;
      if (EXEMPT.test(rawLines[k])) return;
      findings.push(`${relative(ROOT, path)}:${k + 1}  ${rawLines[k].trim().slice(0, 120)}`);
    });
}

if (findings.length > 0) {
  console.error(
    `[audit:copy] FAIL — ${findings.length} dash${findings.length === 1 ? "" : "es"} in copy. ` +
      `Use a full stop, a comma, a colon or brackets. A dash that has to stay ` +
      `carries \`copy-exempt: <reason>\` on the same line.\n`,
  );
  for (const f of findings) console.error("  " + f);
  process.exit(1);
}

console.log("[audit:copy] PASS — no em dash or en dash in anything a person reads.");
