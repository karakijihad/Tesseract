#!/usr/bin/env node
/**
 * Copy Silero VAD + ONNX Runtime WASM assets into `public/vad/` so
 * Vite serves them at `/vad/*` in dev and bundles them into `dist/vad/*`
 * at build time. Phase 16 S2 — `lib/voice/vad.ts` pins
 * `baseAssetPath = '/vad/'`, so this hard-codes the contract.
 *
 * The assets are NOT committed to git (they're 36 MB of binary). This
 * script runs as `postinstall` so every fresh clone + `pnpm install`
 * lands them automatically. Tauri / production builds inherit them
 * via Vite's `public/` static-serve.
 *
 * If the source paths change (package upgrade), update `ASSETS` below.
 */
import { copyFileSync, existsSync, mkdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const MIRROR_ROOT = resolve(__dirname, '..');
const TARGET_DIR = join(MIRROR_ROOT, 'public', 'vad');
const require = createRequire(import.meta.url);

/**
 * pnpm flattens packages under `node_modules/.pnpm/<name>@<ver>/...`,
 * so a hardcoded `node_modules/<name>/dist` path won't resolve for
 * transitive deps like `onnxruntime-web`. Use `require.resolve` against
 * each package's `package.json` to locate the physical install dir,
 * then derive `dist/`. Works under both pnpm's symlinked layout and
 * npm's flat layout.
 */
/**
 * Locate a package's `dist/` directory. Some packages (onnxruntime-web)
 * use a restrictive `exports` field that blocks `pkg/package.json`
 * resolution, so fall back to resolving the package's main entry and
 * walking up to its install root.
 */
function pkgDist(pkgName, fromDir = MIRROR_ROOT) {
  try {
    const pkgJson = require.resolve(`${pkgName}/package.json`, { paths: [fromDir] });
    return join(dirname(pkgJson), 'dist');
  } catch {
    // exports-restricted; resolve the main entry and walk to the
    // package root by searching upward for the matching package.json.
    const entry = require.resolve(pkgName, { paths: [fromDir] });
    let cur = dirname(entry);
    for (let i = 0; i < 8; i += 1) {
      const candidate = join(cur, 'package.json');
      if (existsSync(candidate)) {
        const root = dirname(candidate);
        // Confirm we landed on the right package, not a parent.
        if (root.endsWith(pkgName.replace('/', join('', '')))) return join(root, 'dist');
        return join(root, 'dist');
      }
      const next = dirname(cur);
      if (next === cur) break;
      cur = next;
    }
    throw new Error(`could not locate package root for ${pkgName}`);
  }
}

// `onnxruntime-web` is a transitive dep of `@ricky0123/vad-web` (pnpm
// doesn't hoist it to the top level). Resolve it from vad-web's
// install dir so we hit the same physical copy vad-web pulls in.
const VAD_WEB_DIST = pkgDist('@ricky0123/vad-web');
const VAD_WEB_ROOT = dirname(VAD_WEB_DIST);
const ORT_DIST = pkgDist('onnxruntime-web', VAD_WEB_ROOT);

const ASSETS = [
  [VAD_WEB_DIST, 'silero_vad_v5.onnx'],
  [VAD_WEB_DIST, 'vad.worklet.bundle.min.js'],
  [ORT_DIST, 'ort-wasm-simd-threaded.wasm'],
  [ORT_DIST, 'ort-wasm-simd-threaded.mjs'],
  [ORT_DIST, 'ort-wasm-simd-threaded.jsep.wasm'],
  [ORT_DIST, 'ort-wasm-simd-threaded.jsep.mjs'],
];

mkdirSync(TARGET_DIR, { recursive: true });

let missing = 0;
for (const [srcDir, name] of ASSETS) {
  const src = join(srcDir, name);
  if (!existsSync(src)) {
    console.warn(`[copy-vad-assets] missing source: ${src}`);
    missing += 1;
    continue;
  }
  copyFileSync(src, join(TARGET_DIR, name));
}

if (missing > 0) {
  console.error(
    `[copy-vad-assets] ${missing} asset(s) missing — voice will fail at runtime. ` +
    `Run \`pnpm install\` to refresh the node_modules tree.`,
  );
  process.exitCode = 1;
} else {
  console.log(`[copy-vad-assets] copied ${ASSETS.length} VAD assets → public/vad/`);
}
