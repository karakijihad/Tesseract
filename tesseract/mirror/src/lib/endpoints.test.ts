import { afterEach, describe, expect, it, vi } from 'vitest';

async function loadResolver() {
  vi.resetModules();
  const mod = await import('./endpoints');
  return mod;
}

describe('resolveBackendBase (Tauri-aware)', () => {
  afterEach(() => {
    delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it('returns the Tauri backend when under Tauri and no env override', async () => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    vi.stubEnv('DEV', false);
    const { BACKEND_BASE } = await loadResolver();
    expect(BACKEND_BASE).toBe('http://127.0.0.1:8000');
  });

  it('prefers VITE_TESSERACT_BACKEND over the Tauri fallback', async () => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    vi.stubEnv('DEV', false);
    vi.stubEnv('VITE_TESSERACT_BACKEND', 'http://127.0.0.1:9000');
    const { BACKEND_BASE } = await loadResolver();
    expect(BACKEND_BASE).toBe('http://127.0.0.1:9000');
  });

  it('derives a ws:// URL from the Tauri http base', async () => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    vi.stubEnv('DEV', false);
    const { WS_URL } = await loadResolver();
    expect(WS_URL).toBe('ws://127.0.0.1:8000/ws');
  });
});
