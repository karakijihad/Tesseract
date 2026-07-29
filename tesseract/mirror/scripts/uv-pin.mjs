/**
 * Single source of truth for the pinned `uv` release consumed by both
 * `fetch-uv.mjs` (downloads + verifies it) and `guard-uv.mjs` (verifies
 * the on-disk `uv.exe` before a build bundles it). Keeping one copy of
 * these constants means the build-time guard can never silently drift
 * from what was actually fetched and verified.
 *
 * To bump the version: change UV_VERSION below, run `pnpm run fetch:uv`,
 * and it will fail with both the live-fetched and recorded checksums in
 * the error message. Verify the live one yourself against
 * https://github.com/astral-sh/uv/releases/tag/<version>, then update
 * UV_ZIP_SHA256 (and UV_EXE_SHA256, printed by a successful run) here.
 */
export const UV_VERSION = '0.11.32';
export const UV_ASSET = 'uv-x86_64-pc-windows-msvc.zip';
export const RELEASE_BASE = `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}`;

// Recorded at pin time from `${RELEASE_BASE}/${UV_ASSET}.sha256` — see
// fetch-uv.mjs's header for the two-layer verification this defends.
export const UV_ZIP_SHA256 = 'acfde570451cfdb8689fa159a138ee805ba4e241c466432750302c86254b0984';
// Recorded at pin time from the uv.exe extracted out of the zip verified
// above. This is the pin `guard-uv.mjs` checks the shipped resource
// against at build time, and the fast local skip check `fetch-uv.mjs`
// uses to avoid re-downloading an already-valid binary.
export const UV_EXE_SHA256 = '23cf0f8194ff576562646a1a2950c6826249c8806cd1547debd24db77eb68f58';
