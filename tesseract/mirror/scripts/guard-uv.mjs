#!/usr/bin/env node
/**
 * Build-time guard, wired into `tauri.conf.json`'s `beforeBuildCommand`
 * so `pnpm tauri build` cannot silently bundle a missing or zero-byte
 * `uv.exe` and ship an installer whose first-run provisioning fails on
 * a user's machine (`resources/` is gitignored — see
 * `scripts/fetch-uv.mjs`'s header for why the file isn't in git).
 *
 * Deliberately does NOT fetch — that's `pnpm run fetch:uv`, a separate
 * step, so a build never triggers an unexpected network download.
 */
import { existsSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const OUT = join(resolve(__dirname, '..'), 'src-tauri', 'resources', 'binaries', 'uv.exe');

const size = existsSync(OUT) ? statSync(OUT).size : 0;
if (size === 0) {
  console.error(
    '[guard-uv] tesseract/mirror/src-tauri/resources/binaries/uv.exe is missing or empty — ' +
      'this build would bundle a broken installer that fails on first run. ' +
      'Run `pnpm run fetch:uv` to fetch and verify the pinned build, then retry.',
  );
  process.exitCode = 1;
} else {
  console.log(`[guard-uv] resources/binaries/uv.exe present (${size} bytes) — ok.`);
}
