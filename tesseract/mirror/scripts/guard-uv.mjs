#!/usr/bin/env node
/**
 * Build-time guard, wired into `tauri.conf.json`'s `beforeBuildCommand`
 * so `pnpm tauri build` cannot silently bundle a missing, zero-byte, or
 * tampered/corrupted `uv.exe` and ship an installer whose first-run
 * provisioning fails — or worse, runs an unverified binary — on a
 * user's machine (`resources/` is gitignored — see `scripts/fetch-uv.mjs`'s
 * header for why the file isn't in git).
 *
 * Deliberately does NOT fetch — that's `pnpm run fetch:uv`, a separate
 * step, so a build never triggers an unexpected network download. This
 * script only reads the file already on disk and checks its SHA-256
 * against `UV_EXE_SHA256` in `scripts/uv-pin.mjs` — the SAME pin
 * `fetch-uv.mjs` verifies against on download, so the two scripts can
 * never disagree about what a valid `uv.exe` looks like.
 *
 * This is the fast pre-flight check for `pnpm tauri build` /
 * `pnpm tauri dev`. `pnpm run release:gate` (scripts/release-gate.mjs)
 * is the mandatory pre-release command — it runs this plus the full
 * test/audit suite and MUST pass before cutting a release build.
 */
import { createHash } from 'node:crypto';
import { existsSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { UV_EXE_SHA256 } from './uv-pin.mjs';

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT = join(resolve(__dirname, '..'), 'src-tauri', 'resources', 'binaries', 'uv.exe');

function sha256(buf) {
  return createHash('sha256').update(buf).digest('hex');
}

const size = existsSync(OUT) ? statSync(OUT).size : 0;
if (size === 0) {
  console.error(
    '[guard-uv] tesseract/mirror/src-tauri/resources/binaries/uv.exe is missing or empty — ' +
      'this build would bundle a broken installer that fails on first run. ' +
      'Run `pnpm run fetch:uv` to fetch and verify the pinned build, then retry.',
  );
  process.exitCode = 1;
} else {
  const actual = sha256(readFileSync(OUT));
  if (actual !== UV_EXE_SHA256) {
    console.error(
      `[guard-uv] tesseract/mirror/src-tauri/resources/binaries/uv.exe does not match the pinned ` +
        `checksum (expected ${UV_EXE_SHA256}, got ${actual}) — this build would bundle a tampered ` +
        'or corrupted uv.exe. Run `pnpm run fetch:uv` to re-fetch and re-verify the pinned build, ' +
        'then retry.',
    );
    process.exitCode = 1;
  } else {
    console.log(`[guard-uv] resources/binaries/uv.exe present (${size} bytes) and matches pinned sha256 — ok.`);
  }
}
