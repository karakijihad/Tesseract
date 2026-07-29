#!/usr/bin/env node
/**
 * release-gate.mjs
 *
 * Backing script for `pnpm run release:gate`.
 *
 * **Do not build a release with `pnpm tauri build`. Use
 * `pnpm run release:build`**, which is this gate followed by the build, so
 * the artifact you ship is the artifact the gate passed.
 *
 * This stays OUT of `tauri.conf.json`'s `beforeBuildCommand` on purpose:
 * that hook fires on `pnpm tauri dev` too, and running the whole suite on
 * every dev launch would be unusable. The cost of that choice is that a bare
 * `pnpm tauri build` can still produce an ungated installer — which is
 * exactly what the 2026-07-29 Codex audit (M2) flagged. `release:build`
 * closes it by making the gated path the documented one-command path;
 * `PLAN-03` Task 16 step 2 names it.
 *
 * Runs each check as a child process, in order, failing fast on the
 * first non-zero exit and streaming that child's output live:
 *   1. guard:uv                     — pinned uv.exe SHA-256 check
 *   2. vitest run (`pnpm run test`) — frontend test suite
 *   3. audit:tokens                 — hardcoded design-token audit
 *   4. cargo test                   — Rust unit/integration tests (src-tauri)
 *   5. check_version_consistency    — CL-M9 version-drift gate
 *   6. pytest tests/distributable_app — Python distributable suite
 *
 * Step 6 points `TESSERACT_HOME` at a throwaway temp dir before running,
 * per the project's zero-tolerance rule that tests must never write to
 * `tesseract/logs/`.
 */
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const MIRROR_ROOT = resolve(__dirname, '..');
const SRC_TAURI_ROOT = join(MIRROR_ROOT, 'src-tauri');
const REPO_ROOT = resolve(MIRROR_ROOT, '..', '..');
const TESSERACT_ROOT = join(REPO_ROOT, 'tesseract');

function run(label, command, args, options = {}) {
  console.log(`\n[release-gate] === ${label} ===`);
  const result = spawnSync(command, args, { stdio: 'inherit', shell: true, ...options });
  if (result.error) {
    console.error(`[release-gate] ${label} failed to start: ${result.error.message}`);
    process.exit(1);
  }
  if (result.status !== 0) {
    console.error(`[release-gate] ${label} FAILED (exit ${result.status}) — aborting release gate.`);
    process.exit(result.status ?? 1);
  }
  console.log(`[release-gate] ${label} OK`);
}

function main() {
  run('guard:uv', 'pnpm', ['run', 'guard:uv'], { cwd: MIRROR_ROOT });
  run('vitest run', 'pnpm', ['run', 'test'], { cwd: MIRROR_ROOT });
  run('audit:tokens', 'pnpm', ['run', 'audit:tokens'], { cwd: MIRROR_ROOT });
  run('cargo test', 'cargo', ['test'], { cwd: SRC_TAURI_ROOT });
  run(
    'check_version_consistency',
    'python',
    ['-m', 'tesseract.scripts.check_version_consistency'],
    { cwd: REPO_ROOT },
  );

  const pytestHome = mkdtempSync(join(tmpdir(), 'tesseract-release-gate-'));
  try {
    run(
      'pytest tests/distributable_app',
      'python',
      ['-m', 'pytest', 'tests/distributable_app', '-v'],
      { cwd: TESSERACT_ROOT, env: { ...process.env, TESSERACT_HOME: pytestHome } },
    );
  } finally {
    rmSync(pytestHome, { recursive: true, force: true });
  }

  console.log('\n[release-gate] all checks passed.');
}

main();
