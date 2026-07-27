#!/usr/bin/env node
/**
 * Fetch the pinned `uv.exe` build-time binary that `tauri.conf.json`
 * bundles as a resource and `provision.rs` shells out to for
 * `uv python install` / `uv venv` / `uv pip install` on first run.
 *
 * NOT committed to git (`src-tauri/.gitignore` ignores `resources/` — a
 * ~75 MB third-party binary has no business in this repo's history), so
 * every fresh clone needs `pnpm run fetch:uv` once before `pnpm tauri
 * build` can produce a working installer. `scripts/guard-uv.mjs` is the
 * fast pre-flight that reminds you to do that; it does not fetch itself.
 *
 * Verification: downloads the pinned Windows x86_64 zip from uv's GitHub
 * release, fetches that release's own published `.sha256` checksum file,
 * and requires the downloaded zip's computed SHA256 to match BOTH:
 *   1. the live-fetched checksum (defends against transport corruption
 *      / a bad mirror), and
 *   2. `UV_ZIP_SHA256` recorded below (defends against the release
 *      asset being silently replaced after this pin was set — the live
 *      fetch alone can't catch that, since a re-uploaded asset would
 *      come with its own matching, equally-replaced `.sha256`).
 * Either mismatch aborts loudly before anything is extracted or written.
 *
 * To bump the version: change UV_VERSION below, run this script, and it
 * will fail with both the live-fetched and recorded checksums in the
 * error message. Verify the live one yourself against
 * https://github.com/astral-sh/uv/releases/tag/<version>, then update
 * UV_ZIP_SHA256 (and UV_EXE_SHA256, printed by a successful run) here.
 */
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const MIRROR_ROOT = resolve(__dirname, '..');
const BIN_DIR = join(MIRROR_ROOT, 'src-tauri', 'resources', 'binaries');
const OUT = join(BIN_DIR, 'uv.exe');

const UV_VERSION = '0.11.32';
const UV_ASSET = 'uv-x86_64-pc-windows-msvc.zip';
const RELEASE_BASE = `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}`;

// Recorded at pin time from `${RELEASE_BASE}/${UV_ASSET}.sha256` — see header.
const UV_ZIP_SHA256 = 'acfde570451cfdb8689fa159a138ee805ba4e241c466432750302c86254b0984';
// Recorded at pin time from the uv.exe extracted out of the zip verified
// above. Only used as a fast, local, no-network skip check — never the
// supply-chain gate (that's UV_ZIP_SHA256 against the live download).
const UV_EXE_SHA256 = '23cf0f8194ff576562646a1a2950c6826249c8806cd1547debd24db77eb68f58';

// Windows ships bsdtar (libarchive) at this path, which extracts .zip
// transparently. A `tar` found earlier on PATH may be GNU tar (e.g. Git
// for Windows), which does not support .zip — so this is invoked by
// full path rather than relying on PATH resolution.
const SYSTEM_TAR = 'C:\\Windows\\System32\\tar.exe';

function sha256(buf) {
  return createHash('sha256').update(buf).digest('hex');
}

function fail(msg) {
  throw new Error(`[fetch-uv] ${msg}`);
}

async function download(url) {
  const res = await fetch(url);
  if (!res.ok) fail(`GET ${url} -> ${res.status} ${res.statusText}`);
  return Buffer.from(await res.arrayBuffer());
}

function alreadyValid() {
  if (!existsSync(OUT)) return false;
  if (statSync(OUT).size === 0) return false;
  return sha256(readFileSync(OUT)) === UV_EXE_SHA256;
}

async function main() {
  if (alreadyValid()) {
    console.log(`[fetch-uv] resources/binaries/uv.exe already matches uv ${UV_VERSION} — skipping download.`);
    return;
  }

  console.log(`[fetch-uv] fetching uv ${UV_VERSION} (${UV_ASSET})...`);
  const [zip, checksumText] = await Promise.all([
    download(`${RELEASE_BASE}/${UV_ASSET}`),
    download(`${RELEASE_BASE}/${UV_ASSET}.sha256`).then((b) => b.toString('utf8')),
  ]);

  const published = checksumText.trim().split(/\s+/)[0];
  if (published !== UV_ZIP_SHA256) {
    fail(
      `upstream's published checksum for ${UV_ASSET}@${UV_VERSION} is ${published}, which no ` +
        `longer matches the value recorded in scripts/fetch-uv.mjs (${UV_ZIP_SHA256}). This could ` +
        `mean the release asset was replaced after this pin was set — verify the new checksum ` +
        `yourself at https://github.com/astral-sh/uv/releases/tag/${UV_VERSION} before updating ` +
        `the pin. Refusing to proceed.`,
    );
  }

  const actual = sha256(zip);
  if (actual !== UV_ZIP_SHA256) {
    fail(
      `downloaded ${UV_ASSET} does not match its published checksum ` +
        `(expected ${UV_ZIP_SHA256}, got ${actual}) — refusing to install a corrupted or ` +
        `tampered binary. Try running \`pnpm run fetch:uv\` again.`,
    );
  }

  const work = mkdtempSync(join(tmpdir(), 'tesseract-uv-'));
  try {
    const zipPath = join(work, UV_ASSET);
    writeFileSync(zipPath, zip);
    const tarBin = existsSync(SYSTEM_TAR) ? SYSTEM_TAR : 'tar';
    execFileSync(tarBin, ['-xf', zipPath, '-C', work], { stdio: 'inherit' });

    const extracted = join(work, 'uv.exe');
    if (!existsSync(extracted) || statSync(extracted).size === 0) {
      fail(`extraction of ${UV_ASSET} did not produce a non-empty uv.exe`);
    }

    mkdirSync(BIN_DIR, { recursive: true });
    writeFileSync(OUT, readFileSync(extracted));
  } finally {
    rmSync(work, { recursive: true, force: true });
  }

  console.log(
    `[fetch-uv] wrote ${OUT} (${statSync(OUT).size} bytes, sha256 ${sha256(readFileSync(OUT))}).`,
  );
}

main().catch((err) => {
  console.error(err.message ?? err);
  process.exitCode = 1;
});
