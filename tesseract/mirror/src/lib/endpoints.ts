// Backend endpoint resolution. Config order:
//   1. VITE_TESSERACT_BACKEND (dev or prod override)
//   2. DEV fallback: http://localhost:8000
//   3. Tauri packaged: the local backend the Rust side launched (mirror.yaml default port)
//   4. Browser production fallback: same origin (Mirror served from backend)

export function isTauri(): boolean {
  return typeof window !== 'undefined' && '__TAURI_INTERNALS__' in window;
}

function resolveBackendBase(): string {
  const fromEnv = import.meta.env.VITE_TESSERACT_BACKEND as string | undefined;
  if (fromEnv && fromEnv.length > 0) return fromEnv.replace(/\/+$/, '');
  if (import.meta.env.DEV) return 'http://localhost:8000';
  if (isTauri()) return 'http://127.0.0.1:8000';
  return window.location.origin;
}

export const BACKEND_BASE: string = resolveBackendBase();

export const WS_URL: string = (() => {
  const base = BACKEND_BASE;
  if (base.startsWith('https://')) return `wss://${base.slice('https://'.length)}/ws`;
  if (base.startsWith('http://')) return `ws://${base.slice('http://'.length)}/ws`;
  return `${base}/ws`;
})();

/**
 * Absolutize a backend-relative URL (e.g. `/api/downloads/...`) so it can be
 * used in `<img src>` / `<audio src>` / markdown image references that the
 * browser would otherwise resolve against `window.location.origin`. In dev
 * the Mirror UI runs on a different port from the backend, so a bare
 * `/api/...` href 404s without this rewrite. Already-absolute and data URIs
 * pass through unchanged.
 */
export function backendAssetUrl(href: string): string {
  if (!href) return href;
  if (href.startsWith('data:') || href.startsWith('blob:')) return href;
  if (/^https?:\/\//i.test(href)) return href;
  if (href.startsWith('/')) return `${BACKEND_BASE}${href}`;
  return href;
}
